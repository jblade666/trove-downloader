"""wormhole.app, against a synthetic encrypted room. All data is made up here."""

from __future__ import annotations

import base64
import os

import pytest
import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from trove import ece
from trove.engine import SyncOptions, sync_listing
from trove.errors import PermanentTransferError, ShareError
from trove.providers import _safe_url, is_gone_error, open_url, wormhole

KEY = bytes(range(16))
KEY_TEXT = base64.urlsafe_b64encode(KEY).decode().rstrip("=")
ROOM = "AbCd12"
LINK = f"https://wormhole.app/{ROOM}#{KEY_TEXT}"
B2_TOKEN = "4_b2token/with+chars=="
CREATED = 1_700_000_000
PIECE = 40_000  # small, so files cross pieces and records cross pieces


# -- building a room ---------------------------------------------------------


def encrypt(data: bytes, salt: bytes | None = None) -> bytes:
    """RFC 8188 the way wormhole writes it: 64 KiB records, delimiter 1, last 2."""
    salt = salt or os.urandom(16)
    aead = AESGCM(ece._hkdf(KEY, salt, b"Content-Encoding: aes128gcm\0"))
    nonce_base = ece._hkdf(KEY, salt, b"Content-Encoding: nonce\0", ece.NONCE_LENGTH)
    out = bytearray(salt + ece.RECORD_SIZE.to_bytes(4, "big") + b"\0")
    step = ece.RECORD_SIZE - ece.CHUNK_META_LENGTH
    chunks = [data[i : i + step] for i in range(0, len(data), step)]
    for seq, chunk in enumerate(chunks):
        counter = int.from_bytes(nonce_base[-4:], "big") ^ seq
        nonce = nonce_base[:-4] + counter.to_bytes(4, "big")
        delimiter = b"\x02" if seq == len(chunks) - 1 else b"\x01"
        out += aead.encrypt(nonce, chunk + delimiter, None)
    return bytes(out)


def bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    return b"d" + b"".join(bencode(k) + bencode(v) for k, v in sorted(value.items())) + b"e"


class FakeResponse:
    def __init__(self, status=200, data=None, content=b""):
        self.status_code, self.data, self.content = status, data, content
        self.text = "" if data is None else str(data)

    def json(self):
        if self.data is None:
            raise ValueError("not json")
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)


class FakeRoom:
    """The room API and B2, with a record of every call."""

    def __init__(self, files: dict[str, bytes], cloud_state="uploaded", single=False):
        self.files = files
        self.salt = os.urandom(16)
        self.cloud_state = cloud_state
        self.calls: list[str] = []
        self.room_status = 200
        self.auth_status = 200
        self.piece_status: list[int] = []
        self.piece_error: Exception | None = None
        self.corrupt_piece: int | None = None
        self.share = None

        # Encrypt once, so the torrent's lengths and the stored stream agree.
        blobs = [encrypt(d) for d in files.values()]
        self.stream = b"".join(blobs)
        if single:
            (name,) = files
            info = {"name": name, "length": len(blobs[0]), "piece length": PIECE}
        else:
            info = {
                "files": [{"length": len(b), "path": p.split("/")} for p, b in zip(files, blobs)],
                "name": "ignored",
                "piece length": PIECE,
            }
        torrent = bencode({"creation date": CREATED, "info": info})
        iv = os.urandom(16)
        sealed = AESGCM(ece._hkdf(KEY, self.salt, b"metadata")).encrypt(iv, torrent, None)
        self.encrypted_torrent = base64.b64encode(iv + sealed).decode()

    def install(self, share):
        self.share = share
        share.session.request = self.api
        share.b2.get = self.piece
        return share

    def api(self, method, url, **kwargs):
        path = url.split(f"/room/{ROOM}", 1)[1]
        self.calls.append(f"{method} {path or '/'}")
        if self.room_status != 200:
            return FakeResponse(self.room_status, {})
        if path == "/salt":
            return FakeResponse(200, {"salt": base64.b64encode(self.salt).decode()})
        if self.share.session.headers.get("Authorization") != f"Bearer sync-v1 {ece.auth_token(KEY, self.salt)}":
            return FakeResponse(403, {})
        if path == "":
            return FakeResponse(
                200,
                {
                    "encryptedTorrentFile": self.encrypted_torrent,
                    "cloudState": self.cloud_state,
                    "expiresAtTimestampMs": (CREATED + 86400) * 1000,
                    "remainingDownloads": 4,
                    "maxDownloads": 5,
                },
            )
        if path == "/b2/auth-download":
            return FakeResponse(self.auth_status, {"downloadUrl": "https://b2.example", "authorizationToken": B2_TOKEN})
        if path == "/cloud-state":
            return FakeResponse(200, {"cloudState": self.cloud_state})
        raise AssertionError(path)

    def piece(self, url, params=None, headers=None, timeout=None):
        number = int(url.rsplit("/", 1)[1])
        self.calls.append(f"piece {number}")
        assert params == {"Authorization": B2_TOKEN}
        if self.piece_error:
            error, self.piece_error = self.piece_error, None
            raise error
        if self.piece_status:
            return FakeResponse(self.piece_status.pop(0))
        data = bytearray(self.stream[number * PIECE : (number + 1) * PIECE])
        if number == self.corrupt_piece:
            data[len(data) // 2] ^= 0xFF
        first, last = headers["Range"].removeprefix("bytes=").split("-")
        return FakeResponse(206, content=bytes(data[int(first) : int(last) + 1]))


def room(files, **kwargs):
    return FakeRoom(files, **kwargs)


FILES = {
    "Big: file.bin": os.urandom(150_000),  # several records and pieces
    "Docs/a.pdf": b"tiny",
    "Docs/Deep/empty.txt": b"",
    "Docs/exact.bin": os.urandom(ece.RECORD_SIZE - ece.CHUNK_META_LENGTH),  # exactly one full record
}


def open_room(fake):
    return fake.install(wormhole.open_share(LINK))


def read(fs, path):
    with fs.open(path, "rb") as fp:
        return fp.read()


# -- links -------------------------------------------------------------------


def test_links():
    assert wormhole.matches(LINK)
    assert wormhole.matches(f"wormhole.app/{ROOM}#{KEY_TEXT}")
    assert wormhole.matches(f"https://wormhole.app/{ROOM}")  # matched, so the missing key gets a clear error
    assert not wormhole.matches(f"https://wormhole.example/{ROOM}#{KEY_TEXT}")
    assert not wormhole.matches(f"https://wormhole.app/{ROOM}/more#{KEY_TEXT}")
    assert open_url(LINK).provider_name == "wormhole"


def test_a_bare_room_and_key_with_the_provider_named():
    from trove.providers import bare_links

    assert bare_links(f"{ROOM}#{KEY_TEXT}", "wormhole") == [LINK]
    share = open_url(f"{ROOM}#{KEY_TEXT}", provider="wormhole")
    assert share.provider_name == "wormhole" and ROOM in share.identity
    with pytest.raises(ShareError, match="no key"):
        open_url(ROOM, provider="wormhole")
    assert bare_links(f"wormhole.app/{ROOM}#{KEY_TEXT}", "wormhole") == []  # a host is a link, not a bare id
    assert not wormhole.matches(f"{ROOM}#{KEY_TEXT}")  # never detected without the provider named


@pytest.mark.parametrize("url", [f"https://wormhole.app/{ROOM}", f"https://wormhole.app/{ROOM}#abc", f"https://wormhole.app/{ROOM}#!!!!"])
def test_a_missing_or_bad_key_is_a_clear_error(url):
    with pytest.raises(ShareError) as e:
        wormhole.open_share(url)
    assert not e.value.gone


def test_the_key_never_appears_in_output():
    share = wormhole.open_share(LINK)
    shown = " ".join([*share.describe(), repr(share), share.identity, _safe_url(LINK)])
    assert KEY_TEXT not in shown and ROOM in shown


def test_settings_match_the_files_the_old_tool_wrote():
    share = wormhole.open_share(LINK)
    assert share.colon == "underscore" and not share.can_resume
    assert share.manifest_name is None and share.verify_kind is None


# -- listing -----------------------------------------------------------------


def test_listing_gives_plaintext_sizes_and_the_creation_date():
    fake = room(FILES)
    listing = open_room(fake).list()
    assert listing.share_name == ROOM
    assert {f.path: f.size for f in listing.files} == {p: len(d) for p, d in FILES.items()}
    assert {f.mtime for f in listing.files} == {CREATED}
    assert [(f.path, f.mtime) for f in listing.folders] == [("Docs", CREATED), ("Docs/Deep", CREATED)]
    assert listing.root_mtime is None
    assert "POST /b2/auth-download" not in fake.calls


def test_a_single_file_torrent():
    fake = room({"only.pdf": b"one file"}, single=True)
    listing = open_room(fake).list()
    assert [(f.path, f.size) for f in listing.files] == [("only.pdf", 8)]


def test_a_subfolder_listing():
    listing = open_room(room(FILES)).list("Docs/Deep")
    assert [f.path for f in listing.files] == ["Docs/Deep/empty.txt"]
    assert [f.path for f in listing.folders] == ["Docs/Deep"]


@pytest.mark.parametrize("status,gone", [(410, True), (404, True), (403, False)])
def test_room_errors(status, gone):
    fake = room(FILES)
    fake.room_status = status
    with pytest.raises(ShareError) as e:
        open_room(fake).list()
    assert e.value.gone is gone and is_gone_error(e.value) is gone


def test_a_wrong_key_is_refused():
    fake = room(FILES)
    share = fake.install(wormhole.open_share(f"https://wormhole.app/{ROOM}#{'A' * 22}"))
    with pytest.raises(ShareError):
        share.list()


def test_a_peer_only_room_is_flagged_when_listed(capsys):
    open_room(room(FILES, cloud_state="upload-failed")).list()
    assert "peer-to-peer" in capsys.readouterr().out


# -- syncing -----------------------------------------------------------------


def test_a_sync_decrypts_everything_then_a_rerun_skips_without_authorizing(dest):
    fs, out = dest
    fake = room(FILES)
    share = open_room(fake)
    result = sync_listing(share.list(), share, fs, out, SyncOptions())
    assert result.errors == 0 and result.downloaded == len(FILES)
    for path, data in FILES.items():
        local = path.replace(":", "_")
        assert read(fs, fs.join(out, *local.split("/"))) == data
        assert abs(fs.stat(fs.join(out, *local.split("/"))).mtime - CREATED) < 1
    assert abs(fs.stat(fs.join(out, "Docs", "Deep")).mtime - CREATED) < 1
    assert fake.calls.count("POST /b2/auth-download") == 1

    fake.calls.clear()
    share = open_room(fake)
    result = sync_listing(share.list(), share, fs, out, SyncOptions())
    assert result.skipped == len(FILES) and result.dates_changed == 0 and result.orphans == []
    assert not any(c.startswith(("piece", "POST")) for c in fake.calls)


def test_a_dry_run_never_authorizes(dest):
    fs, out = dest
    fake = room(FILES)
    share = open_room(fake)
    result = sync_listing(share.list(), share, fs, out, SyncOptions(dry_run=True))
    assert result.would_download == len(FILES)
    assert not any(c.startswith(("piece", "POST")) for c in fake.calls)


def test_only_fetches_matching_files(dest):
    fs, out = dest
    fake = room(FILES)
    share = open_room(fake)
    result = sync_listing(share.list(), share, fs, out, SyncOptions(only=("*a.pdf",)))
    assert result.downloaded == 1 and read(fs, fs.join(out, "Docs", "a.pdf")) == b"tiny"


def test_corrupt_data_fails_that_file_and_leaves_nothing(dest):
    fs, out = dest
    fake = room({"x.bin": os.urandom(100_000), "y.txt": b"fine"})
    fake.corrupt_piece = 1
    share = open_room(fake)
    result = sync_listing(share.list(), share, fs, out, SyncOptions(no_orphans=True))
    assert result.errors == 1 and result.downloaded == 1
    assert fs.stat(fs.join(out, "x.bin")) is None
    assert read(fs, fs.join(out, "y.txt")) == b"fine"


def test_a_peer_only_room_stops_on_the_first_download(dest):
    fs, out = dest
    share = open_room(room(FILES, cloud_state="sender-left"))
    with pytest.raises(ShareError, match="peer-to-peer"):
        sync_listing(share.list(), share, fs, out, SyncOptions())


def test_refused_authorization_stops_the_room(dest):
    fs, out = dest
    fake = room(FILES)
    fake.auth_status = 403
    share = open_room(fake)
    with pytest.raises(ShareError, match="authorization"):
        sync_listing(share.list(), share, fs, out, SyncOptions())


def test_an_expired_token_is_renewed_a_limited_number_of_times():
    fake = room({"a.bin": b"data"})
    share = open_room(fake)
    file = share.list().files[0]
    fake.piece_status = [401]
    assert b"".join(share.open_transfer(file).chunks) == b"data"
    assert fake.calls.count("POST /b2/auth-download") == 2

    fake.piece_status = [401] * 5
    with pytest.raises(ShareError):
        b"".join(share.open_transfer(file).chunks)
    assert fake.calls.count("POST /b2/auth-download") == wormhole.MAX_AUTH_CALLS


def test_a_missing_piece_stops_the_room():
    fake = room({"a.bin": b"data"})
    share = open_room(fake)
    file = share.list().files[0]
    fake.piece_status = [404]
    with pytest.raises(ShareError) as e:
        b"".join(share.open_transfer(file).chunks)
    assert e.value.gone


def test_the_storage_token_is_scrubbed_from_errors(monkeypatch):
    monkeypatch.setattr(wormhole, "PIECE_ATTEMPTS", 1)
    fake = room({"a.bin": b"data"})
    share = open_room(fake)
    file = share.list().files[0]
    quoted = requests.utils.quote(B2_TOKEN, safe="")
    fake.piece_error = requests.exceptions.ConnectionError(
        f"Max retries exceeded with url: /file/x/{ROOM}/0?Authorization={quoted} and {B2_TOKEN}"
    )
    with pytest.raises(OSError) as e:
        b"".join(share.open_transfer(file).chunks)
    assert B2_TOKEN not in str(e.value) and quoted not in str(e.value) and "***" in str(e.value)


def test_a_server_ignoring_range_still_gives_the_right_bytes():
    fake = room({"a.bin": os.urandom(90_000)})
    share = open_room(fake)
    file = share.list().files[0]
    original = fake.piece

    def whole_piece(url, params=None, headers=None, timeout=None):
        response = original(url, params, {"Range": f"bytes=0-{PIECE - 1}"}, timeout)
        response.status_code = 200
        return response

    fake.piece = whole_piece
    share.b2.get = whole_piece
    assert b"".join(share.open_transfer(file).chunks) == fake.files["a.bin"]


# -- crypto and bencode ------------------------------------------------------


@pytest.mark.parametrize("size", [0, 1, 65518, 65519, 65520, 131038, 131039, 200_000])
def test_plaintext_size_and_decryption_at_record_boundaries(size):
    data = os.urandom(size)
    blob = encrypt(data)
    assert ece.plaintext_size(len(blob)) == size
    decryptor = ece.EceDecryptor(KEY)
    out = b"".join(decryptor.update(blob[i : i + 7777]) for i in range(0, len(blob), 7777)) + decryptor.finalize()
    assert out == data


def test_a_truncated_stream_is_caught():
    blob = encrypt(os.urandom(100_000))
    decryptor = ece.EceDecryptor(KEY)
    decryptor.update(blob[: ece.HEADER_LENGTH + ece.RECORD_SIZE])
    with pytest.raises(ece.EceError):
        decryptor.finalize()


def test_bdecode():
    assert wormhole.bdecode(bencode({"a": [1, "xy", {"b": -2}]})) == {b"a": [1, b"xy", {b"b": -2}]}
    with pytest.raises(ValueError):
        wormhole.bdecode(b"x")


def test_a_gone_room_stays_gone_when_authorization_is_refused():
    fake = room({"a.bin": b"data"})
    share = open_room(fake)
    file = share.list().files[0]
    fake.auth_status = 410
    with pytest.raises(ShareError) as e:
        b"".join(share.open_transfer(file).chunks)
    assert e.value.gone


def test_a_malformed_torrent_is_a_share_error(monkeypatch):
    fake = room({"a.bin": b"data"})
    monkeypatch.setattr(wormhole, "bdecode", lambda data: {b"info": {}})
    with pytest.raises(ShareError, match="malformed"):
        open_room(fake).list()


def test_a_subfolder_sync_dates_its_folder():
    listing = open_room(room(FILES)).list("Docs")
    assert listing.root_mtime == CREATED
    assert open_room(room(FILES)).list().root_mtime is None
