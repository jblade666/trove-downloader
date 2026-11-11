"""RFC 8188 streaming decryption and the key derivation wormhole.app uses with it.

A port of the decrypt side of https://github.com/SocketDev/wormhole-crypto
(lib/keychain.js, lib/ece.js). The 16-byte main key comes from the share link's
fragment. Everything else is derived from it with HKDF-SHA256:

  * the room salt (from the API) derives the reader auth token and metadata key;
  * each file's own salt (in its 21-byte header) derives its AES-128-GCM key and
    nonce base.
"""

import base64

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_LENGTH = 16
TAG_LENGTH = 16
NONCE_LENGTH = 12
META_IV_LENGTH = 16
RECORD_SIZE = 64 * 1024
HEADER_LENGTH = KEY_LENGTH + 4 + 1  # salt + record size + idlen
CHUNK_META_LENGTH = TAG_LENGTH + 1  # per-record overhead: tag and delimiter


class EceError(Exception):
    """Encrypted data is malformed or failed authentication."""


def b64decode(value: str) -> bytes:
    """Decode standard or URL-safe base64, with or without padding."""
    value = value.replace("-", "+").replace("_", "/")
    return base64.b64decode(value + "=" * (-len(value) % 4), validate=True)


def _hkdf(key: bytes, salt: bytes, info: bytes, length: int = KEY_LENGTH) -> bytes:
    return HKDF(hashes.SHA256(), length, salt, info).derive(key)


def auth_token(main_key: bytes, room_salt: bytes) -> str:
    """The reader token proving we hold the key, without revealing it."""
    return base64.b64encode(_hkdf(main_key, room_salt, b"authentication")).decode()


def decrypt_metadata(main_key: bytes, room_salt: bytes, encrypted: bytes) -> bytes:
    """Decrypt the room's .torrent: a 16-byte IV followed by AES-GCM ciphertext."""
    key = _hkdf(main_key, room_salt, b"metadata")
    iv, ciphertext = encrypted[:META_IV_LENGTH], encrypted[META_IV_LENGTH:]
    try:
        return AESGCM(key).decrypt(iv, ciphertext, None)
    except (InvalidTag, ValueError) as e:
        raise EceError("metadata failed authentication") from e


def plaintext_size(encrypted_size: int) -> int:
    """Size of a file once decrypted (the torrent only records encrypted sizes)."""
    records_size = encrypted_size - HEADER_LENGTH
    if records_size < 0:
        raise EceError(f"encrypted size {encrypted_size} is smaller than the header")
    # -(-a // b) is ceil(a / b) without going through float
    return records_size - CHUNK_META_LENGTH * -(-records_size // RECORD_SIZE)


class EceDecryptor:
    """Incrementally decrypt one file: update() per chunk, then finalize().

    The last record has a different delimiter and only the end of the stream
    identifies it, so a complete record stays buffered until more data arrives.
    Every record is authenticated, and a truncated stream fails on that
    delimiter check, so output that decrypts fully is known to be intact.
    """

    def __init__(self, main_key: bytes):
        self._main_key = main_key
        self._buf = bytearray()
        self._aead = None
        self._nonce_base = b""
        self._seq = 0

    def update(self, data: bytes) -> bytes:
        self._buf += data
        if self._aead is None:
            if len(self._buf) < HEADER_LENGTH:
                return b""
            self._read_header(bytes(self._buf[:HEADER_LENGTH]))
            del self._buf[:HEADER_LENGTH]

        out = bytearray()
        pos = 0
        while len(self._buf) - pos > RECORD_SIZE:
            record = bytes(self._buf[pos : pos + RECORD_SIZE])
            out += self._open_record(record, is_last=False)
            pos += RECORD_SIZE
        del self._buf[:pos]
        return bytes(out)

    def finalize(self) -> bytes:
        if self._aead is None:
            raise EceError("stream ended before the end of the header")
        if not self._buf:
            return b""  # an empty file is a header with no records
        out = self._open_record(bytes(self._buf), is_last=True)
        self._buf.clear()
        return out

    def _read_header(self, header: bytes):
        salt = header[:KEY_LENGTH]
        record_size = int.from_bytes(header[KEY_LENGTH : KEY_LENGTH + 4], "big")
        if record_size != RECORD_SIZE:
            raise EceError(f"unexpected record size {record_size}")
        if header[KEY_LENGTH + 4] != 0:
            raise EceError("non-zero idlen is not supported")
        key = _hkdf(self._main_key, salt, b"Content-Encoding: aes128gcm\0")
        self._nonce_base = _hkdf(
            self._main_key, salt, b"Content-Encoding: nonce\0", NONCE_LENGTH
        )
        self._aead = AESGCM(key)

    def _open_record(self, record: bytes, is_last: bool) -> bytes:
        if self._seq > 0xFFFFFFFF:
            raise EceError("record sequence number exceeds limit")
        counter = int.from_bytes(self._nonce_base[-4:], "big") ^ self._seq
        nonce = self._nonce_base[:-4] + counter.to_bytes(4, "big")
        try:
            padded = self._aead.decrypt(nonce, record, None)
        except InvalidTag as e:
            raise EceError(
                f"record {self._seq} failed authentication (corrupt data or wrong key)"
            ) from e
        self._seq += 1

        data = padded.rstrip(b"\0")
        if not data:
            raise EceError("no delimiter found in record")
        if data[-1] != (2 if is_last else 1):
            raise EceError(f"bad record delimiter {data[-1]} (truncated data?)")
        return data[:-1]
