"""wormhole.app rooms.

A link looks like ``https://wormhole.app/<room>#<key>``. The key is 16 bytes of base64url
and never goes to the server. Everything is end-to-end encrypted:

1. ``GET /api/room/<room>/salt`` gives the room salt. HKDF-SHA256 of key and salt gives
   a reader token (info ``authentication``) and the metadata key (info ``metadata``).
2. ``GET /api/room/<room>`` with ``Authorization: Bearer sync-v1 <token>`` gives the
   encrypted .torrent, the cloud state, the expiry and the downloads left. An unknown
   room is 404 or 410, and a wrong key 403.
3. The torrent lists the **encrypted** sizes. The files are concatenated with no
   padding and stored on Backblaze B2 as pieces of ``piece length`` bytes.
4. ``POST /api/room/<room>/b2/auth-download`` gives a B2 URL and token. It may spend one
   of the room's limited downloads, so it is requested on the first piece read and
   never before: a dry run or an all-skip run doesn't reach it.
5. Each file is an RFC 8188 stream (see trove.ece), so the plaintext size is worked out
   from the encrypted one, and a file can't resume part-way.

Rules:

* The key stays in the fragment and out of output. ``requests`` quotes URLs in its
  messages, and a piece URL carries the B2 token, so piece errors are scrubbed.
* Rooms too big for the cloud, or whose upload failed, are only available
  peer-to-peer from the sender. That isn't supported, and stops the room.
* Torrents have no per-file dates. Files and folders get the torrent's creation date.
* The local folder is named after the room ID, as the old tool did.
"""

from __future__ import annotations

import binascii
import re
import time
from datetime import datetime
from typing import Iterator
from urllib.parse import urlparse

import requests

from trove import ece
from trove.errors import CorruptDownload, PermanentTransferError, ShareError
from trove.http import new_session, request_with_retry
from trove.model import Listing, RemoteFile, RemoteFolder, Share, Transfer

HOST = "https://wormhole.app"
API = f"{HOST}/api"
B2_BUCKET = "socket-dev-prod"
_ROOM = re.compile(r"^/([A-Za-z0-9_-]+)/?$")
_TOKEN_PARAM = re.compile(r"(Authorization=)[^&\s'\")]+")

POLL_INTERVAL = 5  # seconds; the pace the site polls at
UPLOAD_WAIT = 30 * 60  # seconds to wait for a sender who is still preparing or uploading
CLOUD_FAILED = ("upload-failed", "sender-left")
PIECE_TIMEOUT = 60
PIECE_ATTEMPTS = 10  # the retry budget the site gives a piece
MAX_PIECE_BACKOFF = 30
MAX_AUTH_CALLS = 3  # each one may spend one of the room's downloads


def _parse(url: str) -> tuple[str, str] | None:
    """``(room, key text)``. The key may be empty, so a keyless link still matches and
    gets a clear error rather than "no provider"."""
    raw = url.strip()
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    host = parsed.netloc.lower()
    if host not in ("wormhole.app", "www.wormhole.app"):
        return None
    match = _ROOM.match(parsed.path)
    return (match.group(1), parsed.fragment) if match else None


def matches(url: str) -> bool:
    return _parse(url) is not None


def bare_url(text: str) -> str | None:
    """The link a bare ``ROOM#KEY`` (or a keyless ``ROOM``, for a clear error) stands
    for; see providers.bare_links."""
    return f"{HOST}/{text}" if re.fullmatch(r"[A-Za-z0-9_-]+(#[A-Za-z0-9_-]*)?", text) else None


def open_share(url: str, password: str = "") -> "WormholeShare":
    parts = _parse(url)
    if not parts:
        raise ValueError("Expected a link like https://wormhole.app/ROOM#KEY")
    room, key_text = parts
    if password:
        raise ShareError("wormhole.app rooms don't take a password; the key is the part after #.")
    if not key_text:
        raise ShareError("This wormhole link has no key. The full link, including the part after #, is needed.")
    try:
        key = ece.b64decode(key_text)
    except (binascii.Error, ValueError):
        raise ShareError("The key in this wormhole link isn't valid base64.") from None
    if len(key) != ece.KEY_LENGTH:
        raise ShareError(f"The key in this wormhole link should be {ece.KEY_LENGTH} bytes, not {len(key)}.")
    return WormholeShare(room, key)


class WormholeShare(Share):
    provider_name = "wormhole"
    colon = "underscore"
    can_resume = False  # a file decrypts as one stream

    def __init__(self, room: str, key: bytes):
        self.room = room
        self._key = key
        self._salt: bytes | None = None
        self.session = new_session()
        self.session.headers.update({"Origin": HOST, "Referer": f"{HOST}/"})
        self.b2 = new_session()  # no room Authorization header on storage requests
        self._cloud_state = ""
        self._piece_length = 0
        self._spans: dict[str, tuple[int, int]] = {}  # file key -> (offset, encrypted size)
        self._b2_url: str | None = None
        self._b2_token: str | None = None
        self._auth_calls = 0

    def __repr__(self) -> str:
        return f"WormholeShare(room={self.room!r})"

    @property
    def identity(self) -> str:
        return f"wormhole:{self.room}"

    def describe(self) -> list[str]:
        return [f"Wormhole room: {self.room}"]

    # -- room API -------------------------------------------------------------

    def _api(self, method: str, path: str = "") -> dict:
        try:
            response = request_with_retry(self.session, method, f"{API}/room/{self.room}{path}")
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):
                raise ShareError(
                    "The wormhole room is gone: it expired, was deleted, or reached its download limit.", gone=True
                ) from None
            if status in (401, 403):
                raise ShareError("The server refused the key; check the part of the link after #.") from None
            raise
        try:
            return response.json()
        except ValueError:
            raise ShareError(f"The wormhole API sent something other than JSON (HTTP {response.status_code}).") from None

    def _authenticate(self) -> None:
        if self._salt is None:
            self._salt = ece.b64decode(self._api("GET", "/salt")["salt"])
            token = ece.auth_token(self._key, self._salt)
            self.session.headers["Authorization"] = f"Bearer sync-v1 {token}"

    def probe(self) -> None:
        self._authenticate()
        self._api("GET")

    def _room(self) -> dict:
        """The room, waiting while the sender is still encrypting their files."""
        self._authenticate()
        deadline = time.monotonic() + UPLOAD_WAIT
        while True:
            room = self._api("GET")
            if room.get("encryptedTorrentFile"):
                return room
            if time.monotonic() > deadline:
                raise ShareError("The sender never finished preparing the files.")
            print("  The sender is still preparing the files; waiting...")
            time.sleep(POLL_INTERVAL)

    def list(self, subpath: str = "") -> Listing:
        room = self._room()
        try:
            torrent = bdecode(ece.decrypt_metadata(self._key, self._salt, ece.b64decode(room["encryptedTorrentFile"])))
            files, folders = self._parse_torrent(torrent)
        except ece.EceError as e:
            raise ShareError(f"Could not decrypt the file list: {e}") from None
        except (ValueError, IndexError, KeyError, TypeError, AttributeError) as e:
            raise ShareError(f"The room's file list is malformed: {e}") from None

        self._cloud_state = room.get("cloudState") or ""
        if room.get("expiresAtTimestampMs"):
            print(f"  Expires: {datetime.fromtimestamp(room['expiresAtTimestampMs'] / 1000):%Y-%m-%d %H:%M}")
        if room.get("maxDownloads") is not None:
            print(f"  Downloads left: {room.get('remainingDownloads')} of {room['maxDownloads']}")
        if self._cloud_state in CLOUD_FAILED:
            print(
                f"  WARNING: the files aren't on Wormhole's servers ({self._cloud_state}). They are only "
                "available peer-to-peer from the sender, which isn't supported, so downloads will fail."
            )

        sub = subpath.strip("/")
        if not sub:
            # The room's own folder gets no date, as the old tool left it.
            return Listing(files, folders, share_name=self.room)
        prefix = sub + "/"
        files = [f for f in files if f.path.startswith(prefix)]
        folders = [f for f in folders if f.path == sub or f.path.startswith(prefix)]
        root = next((f.mtime for f in folders if f.path == sub), None)
        return Listing(files, folders, share_name=self.room, root_mtime=root)

    def _parse_torrent(self, torrent: dict) -> tuple[list[RemoteFile], list[RemoteFolder]]:
        info = torrent[b"info"]
        created = torrent.get(b"creation date")
        mtime = float(created) if created else None
        self._piece_length = info[b"piece length"]
        # A single-file torrent has name and length instead of a files list.
        entries = info.get(b"files") or [{b"length": info[b"length"], b"path": [info[b"name"]]}]

        files: list[RemoteFile] = []
        folders: set[str] = set()
        offset = 0
        for index, entry in enumerate(entries):
            encrypted = entry[b"length"]
            # A torrent path is a list of names, so a slash inside one is part of the name.
            parts = [p.decode("utf-8", "replace").replace("/", "_") for p in entry[b"path"]]
            key = str(index)
            self._spans[key] = (offset, encrypted)
            files.append(RemoteFile("/".join(parts), ece.plaintext_size(encrypted), mtime, key=key))
            for depth in range(1, len(parts)):
                folders.add("/".join(parts[:depth]))
            offset += encrypted
        return files, [RemoteFolder(p, mtime) for p in sorted(folders)]

    # -- downloads ------------------------------------------------------------

    def open_transfer(self, file: RemoteFile, offset: int = 0) -> Transfer:
        start, length = self._spans[file.key]
        return Transfer(self._decrypt(start, length), 0)

    def _decrypt(self, start: int, length: int) -> Iterator[bytes]:
        decryptor = ece.EceDecryptor(self._key)
        try:
            for chunk in self._read_range(start, length):
                data = decryptor.update(chunk)
                if data:
                    yield data
            tail = decryptor.finalize()
        except ece.EceError as e:
            raise CorruptDownload(f"decryption failed: {e}") from None
        if tail:
            yield tail

    def _read_range(self, offset: int, length: int) -> Iterator[bytes]:
        while length > 0:
            piece, start = divmod(offset, self._piece_length)
            count = min(length, self._piece_length - start)
            yield self._read_piece(piece, start, count)
            offset += count
            length -= count

    def _authorize(self) -> None:
        """Get a B2 token. Only ever called from a real piece read."""
        if self._auth_calls >= MAX_AUTH_CALLS:
            raise ShareError("Wormhole keeps refusing download authorization.")
        self._auth_calls += 1
        if self._auth_calls == 1:
            self._wait_for_cloud()
        try:
            auth = self._api("POST", "/b2/auth-download")
        except ShareError as e:
            raise ShareError(
                f"Download authorization failed: {e} It may have reached its download limit.", gone=e.gone
            ) from None
        self._b2_url, self._b2_token = auth["downloadUrl"], auth["authorizationToken"]

    def _wait_for_cloud(self) -> None:
        deadline = time.monotonic() + UPLOAD_WAIT
        state = self._cloud_state
        while state != "uploaded":
            if state in CLOUD_FAILED or time.monotonic() > deadline:
                raise ShareError(
                    f"The files aren't on Wormhole's servers (state: {state or 'unknown'}). They can only be "
                    "fetched peer-to-peer from the sender, which isn't supported."
                )
            print(f"  The sender is still uploading ({state}); waiting...")
            time.sleep(POLL_INTERVAL)
            state = self._api("GET", "/cloud-state").get("cloudState") or ""
        self._cloud_state = state

    def _scrub(self, text: str) -> str:
        """Remove the B2 token, raw or URL-encoded, from an error message."""
        text = _TOKEN_PARAM.sub(r"\g<1>***", text)
        return text.replace(self._b2_token, "***") if self._b2_token else text

    def _read_piece(self, piece: int, start: int, count: int) -> bytes:
        if self._b2_token is None:
            self._authorize()
        # A Range is harmless on a whole piece, and the last piece is shorter anyway.
        headers = {"Range": f"bytes={start}-{start + count - 1}"}
        failure = ""
        for attempt in range(PIECE_ATTEMPTS):
            if attempt:
                time.sleep(min(2**attempt, MAX_PIECE_BACKOFF))
            url = f"{self._b2_url}/file/{B2_BUCKET}/{self.room}/{piece}"
            try:
                response = self.b2.get(
                    url, params={"Authorization": self._b2_token}, headers=headers, timeout=PIECE_TIMEOUT
                )
            except requests.exceptions.RequestException as e:
                failure = self._scrub(f"{e.__class__.__name__}: {e}")
                continue
            status = response.status_code
            if status in (401, 403):
                failure = f"HTTP {status} (authorization expired?)"
                self._b2_token = None
                self._authorize()
            elif status == 404:
                raise ShareError(f"Piece {piece} of the room is gone (HTTP 404).", gone=True)
            elif status in (200, 206):
                # A server may ignore Range and send the whole piece.
                data = response.content if status == 206 else response.content[start : start + count]
                if len(data) == count:
                    return data
                failure = f"short read: {len(data)} of {count} bytes"
            elif status in (408, 429) or status >= 500:
                failure = f"HTTP {status}"
            else:
                raise PermanentTransferError(f"piece {piece}: HTTP {status}")
        raise IOError(f"piece {piece}: gave up after {PIECE_ATTEMPTS} attempts ({failure})")


# -- bencode ------------------------------------------------------------------


def bdecode(data: bytes):
    return _bdecode_at(data, 0)[0]


def _bdecode_at(data: bytes, i: int):
    c = data[i : i + 1]
    if c == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1 : end]), end + 1
    if c == b"l":
        i += 1
        items = []
        while data[i : i + 1] != b"e":
            if i >= len(data):
                raise ValueError("unterminated list")
            item, i = _bdecode_at(data, i)
            items.append(item)
        return items, i + 1
    if c == b"d":
        i += 1
        items = {}
        while data[i : i + 1] != b"e":
            if i >= len(data):
                raise ValueError("unterminated dictionary")
            k, i = _bdecode_at(data, i)
            items[k], i = _bdecode_at(data, i)
        return items, i + 1
    if not c.isdigit():
        raise ValueError(f"unexpected byte {c!r} at {i}")
    colon = data.index(b":", i)
    end = colon + 1 + int(data[i:colon])
    if end > len(data):
        raise ValueError("string runs past the end")
    return data[colon + 1 : end], end
