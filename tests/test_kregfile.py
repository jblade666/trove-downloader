from __future__ import annotations

import base64
import hashlib
import json

import pytest

from conftest import FakeShare
from trove.engine import SyncOptions, sync_listing
from trove.errors import ShareError
from trove.model import Listing, RemoteFile
from trove.providers import allsync, kregfile, open_url

ROOM = "https://files.example:8443/r/AbC123xyz"


# -- links -------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [ROOM, ROOM + "/", "files.example:8443/r/AbC123xyz", "http://files.example/r/room_1"],
)
def test_room_links_match(url):
    assert kregfile.matches(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://files.example/s/TOKEN",
        "https://files.example/r/",
        "https://files.example/r/abc/extra",
        "https://files.example/r/abc?x=1",
        "https://files.example/room/abc",
        "ftp://files.example/r/abc",
    ],
)
def test_other_links_do_not_match(url):
    assert not kregfile.matches(url)


def test_a_room_link_opens_as_kregfile_and_not_allsync():
    assert not allsync.matches(ROOM)
    share = open_url(ROOM)
    assert share.provider_name == "kregfile"
    assert share.identity == "kregfile:files.example:8443/AbC123xyz"
    assert share.base == "https://files.example:8443"


def test_a_password_is_refused():
    with pytest.raises(ShareError):
        kregfile.open_share(ROOM, "secret")


def test_settings_match_the_files_the_old_tool_wrote():
    share = kregfile.open_share(ROOM)
    assert share.colon == "underscore"
    assert share.manifest_name == ".kregfile-manifest.json"
    assert share.verify_kind == "blake2b-240"
    assert share.stable_names and share.can_resume


# -- listing -----------------------------------------------------------------


def fake_room(share, entries, config=None):
    share._room_page = lambda: "abc123"
    share._snapshot = lambda cv: (list(entries), (config or {}).get("roomname", ""))


def test_listing_converts_the_snapshot():
    share = kregfile.open_share(ROOM)
    fake_room(
        share,
        [
            {"key": "k2", "name": "b/c.pdf", "size": 5, "hash": "H2", "uploaded": 1_600_000_002_000},
            {"key": "k1", "name": "a: one.pdf", "size": 3, "hash": "H1", "uploaded": 1_600_000_001_500},
            {"key": "k3", "name": "", "size": 1, "uploaded": None},
        ],
        {"roomname": "A Room"},
    )
    listing = share.list()
    assert listing.share_name == "A Room" and listing.folders == []
    assert [(f.path, f.key) for f in listing.files] == [("k3", "k3"), ("a: one.pdf", "k1"), ("b_c.pdf", "k2")]
    one = listing.files[1]
    assert one.size == 3 and one.mtime == 1_600_000_001.5 and one.etag == "H1"
    assert listing.files[0].mtime is None


def test_a_room_has_no_subfolders():
    share = kregfile.open_share(ROOM)
    fake_room(share, [])
    with pytest.raises(ShareError):
        share.list("Sub")


def test_a_missing_room_name_gives_none():
    share = kregfile.open_share(ROOM)
    fake_room(share, [])
    assert share.list().share_name is None


def test_downloads_go_by_key_with_the_name_quoted():
    share = kregfile.open_share(ROOM)
    share._verify = "/bundle.pem"
    seen = {}

    def fake_transfer(session, url, offset, **kwargs):
        seen.update(url=url, offset=offset, **kwargs)

    import trove.providers.kregfile as module

    original = module.open_http_transfer
    module.open_http_transfer = fake_transfer
    try:
        share.open_transfer(RemoteFile("a b#?.pdf", key="KEY1"), 7)
    finally:
        module.open_http_transfer = original
    assert seen == {"url": "https://files.example:8443/g/KEY1/a%20b%23%3F.pdf", "offset": 7, "verify": "/bundle.pem"}


# -- stable names (engine) ---------------------------------------------------


class RoomShare(FakeShare):
    """Files keyed like a kregfile room: content by key, names may repeat."""

    provider_name = "room"
    colon = "underscore"
    manifest_name = ".room-manifest.json"
    verify_kind = "blake2b-240"
    stable_names = True

    def __init__(self, uploads: list[tuple[str, str, bytes]]):
        super().__init__()
        self.uploads = uploads  # (key, name, data)

    def open_transfer(self, file, offset=0):
        self.contents = {file.path: dict((k, d) for k, _n, d in self.uploads)[file.key]}
        return super().open_transfer(file, offset)

    def listing(self) -> Listing:
        return Listing(
            [RemoteFile(n, len(d), 1_600_000_000.0, blake(d), key=k) for k, n, d in self.uploads], [], "Room"
        )


def blake(data: bytes) -> str:
    return base64.b64encode(hashlib.blake2b(data, digest_size=30).digest()).decode("ascii")


def read(fs, path):
    with fs.open(path, "rb") as fp:
        return fp.read()


def manifest(fs, out):
    return json.loads(read(fs, fs.join(out, ".room-manifest.json")))


def test_uploads_sharing_a_name_are_kept_apart(dest):
    fs, out = dest
    share = RoomShare([("k1", "Map.png", b"first"), ("k2", "map.png", b"second"), ("k3", "a:b.pdf", b"x")])
    result = sync_listing(share.listing(), share, fs, out, SyncOptions())
    assert result.downloaded == 3 and result.errors == 0
    assert read(fs, fs.join(out, "Map.png")) == b"first"
    assert read(fs, fs.join(out, "map [k2].png")) == b"second"
    assert manifest(fs, out) == {"k1": "Map.png", "k2": "map [k2].png", "k3": "a_b.pdf"}
    assert result.orphans == []


def test_a_new_upload_never_takes_an_expired_uploads_name(dest):
    fs, out = dest
    share = RoomShare([("k1", "Map.png", b"first"), ("k2", "Map.png", b"second")])
    sync_listing(share.listing(), share, fs, out, SyncOptions())
    # k1 expires; a new upload with the same name and size arrives first in the list.
    share.uploads = [("k3", "Map.png", b"third"), ("k2", "Map.png", b"second")]
    share.transfers.clear()
    result = sync_listing(share.listing(), share, fs, out, SyncOptions())
    assert result.skipped == 1 and result.downloaded == 1
    assert read(fs, fs.join(out, "Map [k3].png")) == b"third"
    assert read(fs, fs.join(out, "Map.png")) == b"first"
    assert manifest(fs, out) == {"k1": "Map.png", "k2": "Map [k2].png", "k3": "Map [k3].png"}
    assert result.orphans == ["Map.png"]


def test_a_new_key_never_takes_a_name_a_listed_key_holds(dest):
    fs, out = dest
    share = RoomShare([("k1", "Map.png", b"first")])
    sync_listing(share.listing(), share, fs, out, SyncOptions())
    share.uploads = [("k9", "Map.png", b"newer"), ("k1", "Map.png", b"first")]
    sync_listing(share.listing(), share, fs, out, SyncOptions())
    assert read(fs, fs.join(out, "Map.png")) == b"first"
    assert read(fs, fs.join(out, "Map [k9].png")) == b"newer"


def test_a_dry_run_writes_no_name_manifest(dest):
    fs, out = dest
    share = RoomShare([("k1", "Map.png", b"first")])
    result = sync_listing(share.listing(), share, fs, out, SyncOptions(dry_run=True))
    assert result.would_download == 1
    assert fs.stat(fs.join(out, ".room-manifest.json")) is None


def test_hashes_never_go_into_the_name_manifest(dest):
    fs, out = dest
    share = RoomShare([("k1", "Map.png", b"first")])
    sync_listing(share.listing(), share, fs, out, SyncOptions(verify=True))
    assert manifest(fs, out) == {"k1": "Map.png"}


def test_an_unsafe_manifest_name_is_sanitized(dest):
    fs, out = dest
    share = RoomShare([("k1", "Map.png", b"first")])
    fs.makedirs(out)
    with fs.open(fs.join(out, ".room-manifest.json"), "wb") as fp:
        fp.write(json.dumps({"k1": "../up.png"}).encode())
    sync_listing(share.listing(), share, fs, out, SyncOptions())
    assert read(fs, fs.join(out, ".._up.png")) == b"first"


# -- blake2b verify ----------------------------------------------------------


def test_verify_keeps_a_matching_file_and_replaces_a_changed_one(dest):
    fs, out = dest
    share = RoomShare([("k1", "a.pdf", b"old-data")])
    sync_listing(share.listing(), share, fs, out, SyncOptions())
    share.transfers.clear()
    result = sync_listing(share.listing(), share, fs, out, SyncOptions(verify=True))
    assert result.skipped == 1 and share.transfers == []

    share.uploads = [("k1", "a.pdf", b"new-data")]
    result = sync_listing(share.listing(), share, fs, out, SyncOptions(verify=True))
    assert result.reverified == 1 and result.downloaded == 1
    assert read(fs, fs.join(out, "a.pdf")) == b"new-data"


def test_base64_hashes_compare_case_sensitively(dest):
    fs, out = dest
    share = RoomShare([("k1", "a.pdf", b"contents")])
    sync_listing(share.listing(), share, fs, out, SyncOptions())
    listing = share.listing()
    listing.files[0].etag = listing.files[0].etag.swapcase()
    result = sync_listing(listing, share, fs, out, SyncOptions(verify=True, dry_run=True))
    assert result.reverified == 1


def test_a_download_not_matching_its_hash_is_discarded(dest):
    fs, out = dest
    share = RoomShare([("k1", "a.pdf", b"contents")])
    listing = share.listing()
    listing.files[0].etag = blake(b"other")
    result = sync_listing(listing, share, fs, out, SyncOptions(verify=True))
    assert result.errors == 1 and fs.stat(fs.join(out, "a.pdf")) is None


# -- TLS helpers -------------------------------------------------------------


def test_split_host():
    assert kregfile._split_host("files.example:8443") == ("files.example", 8443)
    assert kregfile._split_host("files.example") == ("files.example", 443)


def test_issuer_urls_are_found_in_raw_certificate_bytes():
    der = b"\x30\x82junk\x86\x1fhttp://ca.example/inter.crt\x00more http://ca.example/inter.crt"
    assert kregfile._issuer_urls(der) == ["http://ca.example/inter.crt"]


def test_a_verified_host_uses_certifi_without_repair(monkeypatch):
    monkeypatch.setattr(kregfile, "_verifies", lambda host, port, cafile: True)
    monkeypatch.setattr(kregfile, "_intermediates", lambda *a: pytest.fail("repaired a good chain"))
    assert kregfile._resolve_verify("files.example:8443") == kregfile.certifi.where()


def test_an_unrepairable_chain_stops_the_share(monkeypatch, tmp_path):
    monkeypatch.setattr(kregfile, "_verifies", lambda host, port, cafile: False)
    monkeypatch.setattr(kregfile, "_cache_path", lambda host: str(tmp_path / "ca" / "h.pem"))
    monkeypatch.setattr(kregfile, "_intermediates", lambda *a: [])
    with pytest.raises(ShareError):
        kregfile._resolve_verify("files.example:8443")
