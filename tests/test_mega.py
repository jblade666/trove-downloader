"""MEGA, against a synthetic encrypted folder. All data is made up here."""

from __future__ import annotations

import base64
import json
import os
import struct

import pytest
import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from conftest import MemFS
from trove.engine import SyncOptions, adopt_existing_names, loose_key, scan_local, sync_listing
from trove.errors import AbortRun, CorruptDownload, PermanentTransferError, ShareError
from trove.model import Listing, RemoteFile, RemoteFolder
from trove.naming import safe_component
from trove.providers import _safe_url, is_gone_error, mega, open_url

FOLDER_KEY = bytes(range(16))
LINK_ID = "PubLink1"
ROOT = "RootNode"


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


LINK = f"https://mega.nz/folder/{LINK_ID}#{b64e(FOLDER_KEY)}"


def ecb(key, data):
    e = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return e.update(data) + e.finalize()


def attrs(key16: bytes, value: dict) -> str:
    raw = b"MEGA" + json.dumps(value).encode()
    raw += b"\0" * (-len(raw) % 16)
    e = Cipher(algorithms.AES(key16), modes.CBC(b"\0" * 16)).encryptor()
    return b64e(e.update(raw) + e.finalize())


def fingerprint(mtime: int) -> str:
    stamp = mtime.to_bytes(8, "little").rstrip(b"\0") or b"\0"
    return b64e(os.urandom(16) + bytes([len(stamp)]) + stamp)


def reference_mac(aes_key: bytes, iv: bytes, data: bytes) -> bytes:
    """A plain, block-at-a-time version of MEGA's MAC, for checking ChunkMac."""
    file_mac = b"\0" * 16
    for start, end in zip([0, *mega._mac_chunk_ends(len(data))], mega._mac_chunk_ends(len(data))):
        chunk_mac = iv + iv
        for off in range(start, end, 16):
            block = data[off : min(off + 16, end)].ljust(16, b"\0")
            chunk_mac = ecb(aes_key, bytes(a ^ b for a, b in zip(chunk_mac, block)))
        file_mac = ecb(aes_key, bytes(a ^ b for a, b in zip(file_mac, chunk_mac)))
    w = struct.unpack(">4I", file_mac)
    return struct.pack(">2I", w[0] ^ w[1], w[2] ^ w[3])


class FakeFile:
    def __init__(self, data: bytes, mtime: int | None = 1_600_000_000):
        self.data, self.mtime = data, mtime
        self.aes, self.iv = os.urandom(16), os.urandom(8)
        mac = reference_mac(self.aes, self.iv, data)
        halves = self.aes + self.iv + mac  # aes ^ second half gives the stored first half
        second = self.iv + mac
        self.key = bytes(a ^ b for a, b in zip(self.aes, second)) + second
        assert mega._file_aes_key(self.key) == self.aes and halves
        counter = self.iv + b"\0" * 8
        e = Cipher(algorithms.AES(self.aes), modes.CTR(counter)).encryptor()
        self.encrypted = e.update(data) + e.finalize()


class FakeMega:
    """The MEGA API and storage for one folder link."""

    def __init__(self, tree: dict[str, FakeFile | None], root_name="Shared Root"):
        self.nodes, self.files, self.calls = [], {}, []
        self.storage_status = 200
        self.api_errors: list[int] = []
        self.corrupt = False
        self.stalls = 0  # downloads that time out before any answer
        self.packets: list[list[dict]] = []
        self._add(ROOT, "", 1, root_name, os.urandom(16))
        handles = {"": ROOT}
        for path in sorted(tree):
            parts = path.split("/")
            for depth in range(1, len(parts)):
                folder = "/".join(parts[:depth])
                if folder not in handles:
                    handles[folder] = f"D{len(handles)}"
                    self._add(handles[folder], handles["/".join(parts[: depth - 1])], 1, parts[depth - 1], os.urandom(16))
            file = tree[path]
            if file is None:  # an explicit empty folder
                handles[path] = f"D{len(handles)}"
                self._add(handles[path], handles["/".join(parts[:-1])], 1, parts[-1], os.urandom(16))
                continue
            handle = f"F{len(self.files)}"
            self.files[handle] = file
            extra = {"c": fingerprint(file.mtime)} if file.mtime else {}
            self._add(handle, handles["/".join(parts[:-1])], 0, parts[-1], file.key, extra, len(file.data))

    def _add(self, handle, parent, t, name, key, extra=None, size=None):
        aes = mega._file_aes_key(key) if t == 0 else key
        node = {"h": handle, "p": parent or "Outside", "t": t, "a": attrs(aes, {"n": name, **(extra or {})}),
                "k": f"Someone:{b64e(os.urandom(16))}/{ROOT}:{b64e(ecb(FOLDER_KEY, key))}", "ts": 1}
        if size is not None:
            node["s"] = size
        self.nodes.append(node)

    def install(self, share):
        share.session.request = self.api
        share.session.get = self.storage
        return share

    def api(self, method, url, params=None, json=None, **kwargs):
        if url.endswith("/sc"):
            self.calls.append("sc")
            assert params["n"] == LINK_ID
            if self.packets:
                return Response(200, {"a": self.packets.pop(0), "sn": "next"})
            return Response(200, {"w": "https://wait.example", "sn": "next"})
        command = json[0]
        self.calls.append(command["a"])
        assert params.get("n") == LINK_ID
        if self.api_errors:
            return Response(200, [self.api_errors.pop(0)])
        if command["a"] == "f":
            return Response(200, [{"f": self.nodes, "ok": [], "sn": "snap"}])
        if command["a"] == "g":
            return Response(200, [{"g": f"https://storage.example/dl/{command['n']}", "s": 1}])
        raise AssertionError(command)

    def storage(self, url, stream=True, timeout=None):
        self.calls.append("download")
        if self.stalls:
            self.stalls -= 1
            raise requests.exceptions.ReadTimeout("read timed out")
        if self.storage_status != 200:
            return Response(self.storage_status, headers={"X-MEGA-Time-Left": "600"})
        handle, _, rng = url.removeprefix("https://storage.example/dl/").partition("/")
        start = int(rng.rstrip("-")) if rng else 0
        data = bytearray(self.files[handle].encrypted[start:])
        if self.corrupt and data:
            data[-1] ^= 1
        return Response(200, content=bytes(data))


class Response:
    def __init__(self, status=200, data=None, content=b"", headers=None):
        self.status_code, self.data, self.content = status, data, content
        self.headers, self.reason = headers or {}, "Reason"

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(str(self.status_code), response=self)

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), 50_000):  # not aligned to 16 or to MAC chunks
            yield self.content[i : i + 50_000]

    def close(self):
        pass


def read(fs, path):
    with fs.open(path, "rb") as fp:
        return fp.read()


TREE = {
    "Top: One.pdf": FakeFile(os.urandom(300_000)),
    " Lead/inner.txt": FakeFile(b"hello"),
    "Dir./deep/x.bin": FakeFile(os.urandom(1_500_000), 1_650_000_000),
    "Empty": None,
    "nodate.txt": FakeFile(b"no fingerprint", None),
    "Dir./.getxfer.123.mega": FakeFile(b""),
}


def open_folder(fake):
    return fake.install(mega.open_share(LINK))


# -- links -------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,kind,ident,sub",
    [
        (LINK, "folder", LINK_ID, ""),
        (f"https://mega.nz/folder/{LINK_ID}#{b64e(FOLDER_KEY)}/folder/SubNode", "folder", LINK_ID, "SubNode"),
        (f"https://mega.nz/#F!{LINK_ID}!{b64e(FOLDER_KEY)}", "folder", LINK_ID, ""),
        (f"https://mega.co.nz/#F!{LINK_ID}!{b64e(FOLDER_KEY)}!SubNode", "folder", LINK_ID, "SubNode"),
        (f"https://mega.nz/file/FileId#{b64e(bytes(32))}", "file", "FileId", ""),
        (f"https://mega.nz/#!FileId!{b64e(bytes(32))}", "file", "FileId", ""),
    ],
)
def test_link_forms(url, kind, ident, sub):
    share = open_url(url)
    assert (share.provider_name, share.kind, share.link_id, share.sub) == ("mega", kind, ident, sub)


def test_old_and_new_forms_are_the_same_share():
    assert open_url(LINK).identity == open_url(f"https://mega.nz/#F!{LINK_ID}!{b64e(FOLDER_KEY)}").identity


@pytest.mark.parametrize(
    "url", ["https://mega.nz/megadrop/abc", "https://mega.nz/filerequest/abc", f"https://mega.nz/folder/{LINK_ID}"]
)
def test_upload_and_keyless_links_say_why(url):
    assert mega.matches(url)
    with pytest.raises(ShareError) as e:
        open_url(url)
    assert not e.value.gone


def test_other_hosts_do_not_match():
    assert not mega.matches(f"https://mega.example/folder/{LINK_ID}#{b64e(FOLDER_KEY)}")


def test_the_key_never_appears_in_output():
    share = open_url(LINK)
    shown = " ".join([*share.describe(), repr(share), share.identity, _safe_url(LINK)])
    assert b64e(FOLDER_KEY) not in shown and LINK_ID in shown


# -- listing -----------------------------------------------------------------


def test_listing_decrypts_names_sizes_and_dates():
    listing = open_folder(FakeMega(TREE)).list()
    assert listing.share_name == "Shared Root" and listing.root_mtime is None
    files = {f.path: (f.size, f.mtime) for f in listing.files}
    assert files == {
        " Lead/inner.txt": (5, 1_600_000_000.0),
        "Dir./deep/x.bin": (1_500_000, 1_650_000_000.0),
        "Top: One.pdf": (300_000, 1_600_000_000.0),
        "nodate.txt": (14, None),
    }
    assert {f.path for f in listing.folders} == {" Lead", "Dir.", "Dir./deep", "Empty"}
    assert all(f.mtime is None for f in listing.folders)


def test_undecryptable_nodes_are_left_out(capsys):
    fake = FakeMega({"a.txt": FakeFile(b"a"), "b.txt": FakeFile(b"b")})
    fake.nodes[-1]["k"] = f"{ROOT}:{b64e(os.urandom(32))}"
    listing = open_folder(fake).list()
    assert [f.path for f in listing.files] == ["a.txt"]
    assert "no key" in capsys.readouterr().out


def test_a_subfolder_link_lists_from_that_folder():
    fake = FakeMega({"A/B/c.txt": FakeFile(b"c"), "A/top.txt": FakeFile(b"t")})
    sub = next(n["h"] for n in fake.nodes if n["t"] == 1 and n["h"] != ROOT and n["p"] != ROOT)
    share = fake.install(mega.open_share(f"{LINK}/folder/{sub}"))
    listing = share.list()
    assert listing.share_name == "B" and [f.path for f in listing.files] == ["c.txt"]


@pytest.mark.parametrize("code,gone", [(-9, True), (-16, True), (-11, False)])
def test_api_errors(code, gone):
    fake = FakeMega(TREE)
    fake.api_errors = [code]
    with pytest.raises(ShareError) as e:
        open_folder(fake).list()
    assert e.value.gone is gone and is_gone_error(e.value) is gone


def test_transient_api_errors_are_retried():
    fake = FakeMega(TREE)
    fake.api_errors = [-3, -18]
    assert open_folder(fake).list().files


def test_an_over_quota_api_answer_stops_the_run():
    fake = FakeMega(TREE)
    share = open_folder(fake)
    file = share.list().files[0]
    fake.api_errors = [-17]
    with pytest.raises(AbortRun):
        share.open_transfer(file)


# -- downloads ---------------------------------------------------------------


def test_downloads_decrypt_and_pass_the_mac():
    fake = FakeMega(TREE)
    share = open_folder(fake)
    for f in share.list().files:
        data = b"".join(share.open_transfer(f).chunks)
        assert data == fake.files[f.key].data


def test_a_resume_from_any_offset_gives_the_right_bytes():
    fake = FakeMega(TREE)
    share = open_folder(fake)
    f = next(f for f in share.list().files if f.size == 1_500_000)
    for offset in (16, 17, 131_073, 1_499_999):
        transfer = share.open_transfer(f, offset)
        assert transfer.offset == offset
        assert b"".join(transfer.chunks) == fake.files[f.key].data[offset:]


def test_corrupt_data_fails_the_mac():
    fake = FakeMega(TREE)
    fake.corrupt = True
    share = open_folder(fake)
    f = next(f for f in share.list().files if f.size == 300_000)
    with pytest.raises(CorruptDownload):
        b"".join(share.open_transfer(f).chunks)


def test_509_stops_the_run():
    fake = FakeMega(TREE)
    share = open_folder(fake)
    f = share.list().files[0]
    fake.storage_status = 509
    with pytest.raises(AbortRun, match="10 minutes"):
        share.open_transfer(f)


def test_a_stalled_download_is_retried_with_a_fresh_url():
    fake = FakeMega(TREE)
    share = open_folder(fake)
    f = next(f for f in share.list().files if f.size == 300_000)
    fake.calls.clear()
    fake.stalls = mega.STALL_TRIES - 1
    assert b"".join(share.open_transfer(f).chunks) == fake.files[f.key].data
    assert fake.calls == ["g", "download"] * mega.STALL_TRIES


def test_a_download_that_always_stalls_stops_the_run():
    fake = FakeMega(TREE)
    share = open_folder(fake)
    f = share.list().files[0]
    fake.calls.clear()
    fake.stalls = mega.STALL_TRIES
    with pytest.raises(AbortRun, match=f"{mega.STALL_TRIES} tries"):
        share.open_transfer(f)
    assert fake.calls.count("g") == mega.STALL_TRIES


def test_a_509_stops_the_run_without_retrying():
    fake = FakeMega(TREE)
    share = open_folder(fake)
    f = share.list().files[0]
    fake.calls.clear()
    fake.storage_status = 509
    with pytest.raises(AbortRun):
        share.open_transfer(f)
    assert fake.calls == ["g", "download"]


def test_a_403_fails_just_that_file():
    fake = FakeMega(TREE)
    share = open_folder(fake)
    f = share.list().files[0]
    fake.storage_status = 403
    with pytest.raises(PermanentTransferError):
        share.open_transfer(f)


@pytest.mark.parametrize("size", [1, 15, 16, 131_072, 131_073, 393_216, 5_000_000])
def test_chunk_mac_matches_the_reference_however_data_arrives(size):
    data = os.urandom(size)
    key, iv = os.urandom(16), os.urandom(8)
    mac = mega.ChunkMac(key, iv, size)
    for i in range(0, size, 77_777):
        mac.update(data[i : i + 77_777])
    assert mac.digest() == reference_mac(key, iv, data)


def test_mac_chunks_grow_to_one_mebibyte():
    ends = list(mega._mac_chunk_ends(6 * 1024 * 1024))
    steps = [b - a for a, b in zip([0, *ends], ends)]
    assert steps[:9] == [131072 * i for i in range(1, 9)] + [1048576]


def test_fingerprint_mtime():
    assert mega.fingerprint_mtime(fingerprint(1_600_000_000)) == 1_600_000_000.0
    assert mega.fingerprint_mtime(b64e(b"short")) is None


# -- syncing with loose names --------------------------------------------------


def test_a_sync_then_a_rerun_skips_everything():
    fs = MemFS()
    fs.makedirs("/out")
    fake = FakeMega(TREE)
    share = open_folder(fake)
    result = sync_listing(share.list(), share, fs, "/out", SyncOptions())
    assert result.errors == 0 and result.downloaded == 4
    assert read(fs, "/out/Top%3a One.pdf") == TREE["Top: One.pdf"].data
    assert read(fs, "/out/Lead/inner.txt") == b"hello"
    assert read(fs, "/out/Dir/deep/x.bin") == TREE["Dir./deep/x.bin"].data
    assert fs.isdir("/out/Empty")

    fake.calls.clear()
    share = open_folder(fake)
    result = sync_listing(share.list(), share, fs, "/out", SyncOptions())
    assert result.skipped == 4 and result.dates_changed == 0 and result.orphans == []
    assert "download" not in fake.calls and "g" not in fake.calls


def test_names_already_stored_differently_are_matched_not_downloaded():
    """A tree written by other tools: NFD, other casing, a kept trailing dot, a
    differently-cased %xx escape and a leading space."""
    fs = MemFS()
    fake = FakeMega({
        "Rhône/Book Of Ages.pdf": FakeFile(b"12345"),
        "Some Long Title./a: b.pdf": FakeFile(b"123"),
        " lead.txt": FakeFile(b"1"),
    })
    nfd = "Rhône"
    for path, data in {f"/out/{nfd}/book of ages.pdf": b"12345", "/out/Some Long Title./a%3A b.pdf": b"123",
                       "/out/ lead.txt": b"1"}.items():
        fs.makedirs(path.rpartition("/")[0])
        with fs.open(path, "wb") as fp:
            fp.write(data)
    share = open_folder(fake)
    result = sync_listing(share.list(), share, fs, "/out", SyncOptions(dry_run=True))
    assert result.would_download == 0 and result.skipped == 3 and result.orphans == []


def test_case_twins_in_a_loosely_named_folder_are_matched_by_size():
    fs = MemFS()
    fake = FakeMega({"Dir /Guide.pdf": FakeFile(b"big content"), "Dir /guide.pdf": FakeFile(b"small")})
    for name, data in {"Guide.pdf": b"big content", "guide (1).pdf": b"small"}.items():
        fs.makedirs("/out/Dir ")
        with fs.open(f"/out/Dir /{name}", "wb") as fp:
            fp.write(data)
    share = open_folder(fake)
    result = sync_listing(share.list(), share, fs, "/out", SyncOptions(dry_run=True))
    assert result.would_download == 0 and result.orphans == []


def test_new_files_go_into_the_folder_already_there():
    fs = MemFS()
    fs.makedirs("/out/Old Name.")
    fake = FakeMega({"old name./new.pdf": FakeFile(b"new")})
    share = open_folder(fake)
    sync_listing(share.list(), share, fs, "/out", SyncOptions())
    assert read(fs, "/out/Old Name./new.pdf") == b"new"


def test_loose_key():
    assert loose_key("A%3a B. ") == loose_key("a: b") == loose_key("  A%3A B")
    assert loose_key("100%20") != loose_key("100 ")
    assert loose_key("Rhône") == loose_key("Rhône")


def test_adoption_leaves_ambiguous_names_alone():
    fs = MemFS()
    for name in ("a.pdf", "A.pdf "):
        fs.makedirs("/out")
        with fs.open(f"/out/{name}", "wb") as fp:
            fp.write(b"x")
    listing = Listing([RemoteFile("a.PDF", 1)], [RemoteFolder("x")])
    listing.files[0].local, listing.folders[0].local = "a.PDF", "x"
    scan = scan_local(fs, "/out", "", open_url(LINK), ())
    adopt_existing_names(listing, scan)
    assert listing.files[0].local == "a.PDF"


def test_percent_naming():
    assert safe_component("The One Ring: Dice?.jpg", "percent") == "The One Ring%3a Dice%3f.jpg"
    assert safe_component("  lead. ", "percent") == "lead"
    assert safe_component("100%.pdf", "percent") == "100%.pdf"


def test_the_shares_own_folder_is_matched_loosely():
    from trove.cli import _existing_folder

    fs = MemFS()
    fs.makedirs("/base/Some Long Title.")
    fs.makedirs("/base/Other")
    assert _existing_folder(fs, "/base", "Some Long Title") == "Some Long Title."
    assert _existing_folder(fs, "/base", "other") == "Other"
    assert _existing_folder(fs, "/base", "New") == "New"
    assert _existing_folder(fs, "/missing", "New") == "New"


def test_a_folder_split_across_two_local_spellings_is_found_in_both():
    fs = MemFS()
    fake = FakeMega({"Archive /a.pdf": FakeFile(b"aaa"), "Archive /b.pdf": FakeFile(b"bb"), "Archive /c.pdf": FakeFile(b"c")})
    for path, data in {"/out/Archive/a.pdf": b"aaa", "/out/Archive /b.pdf": b"bb"}.items():
        fs.makedirs(path.rpartition("/")[0])
        with fs.open(path, "wb") as fp:
            fp.write(data)
    share = open_folder(fake)
    result = sync_listing(share.list(), share, fs, "/out", SyncOptions())
    assert result.skipped == 2 and result.downloaded == 1 and result.orphans == []
    assert read(fs, "/out/Archive/c.pdf") == b"c"


def test_changes_since_the_snapshot_are_replayed():
    """The listing answer can lag; the action packets after its sequence number
    bring it up to date, as MEGAcmd sees it."""
    fake = FakeMega({"keep.txt": FakeFile(b"k"), "Dir/gone.txt": FakeFile(b"g"), "renamed.txt": FakeFile(b"r")})
    extra = FakeMega({"new.txt": FakeFile(b"new!")})
    added = next(n for n in extra.nodes if n["t"] == 0)
    fake.files["NEW"] = extra.files[added["h"]]
    added.update(h="NEW", p=ROOT)
    renamed = next(n for n in fake.nodes if n["h"] == "F2")  # files are numbered in path order
    key = mega._file_aes_key(fake.files[renamed["h"]].key)
    gone_dir = next(n["h"] for n in fake.nodes if n["t"] == 1 and n["h"] != ROOT)
    fake.packets = [
        [{"a": "t", "t": {"f": [added]}}, {"a": "d", "n": gone_dir}],
        [{"a": "u", "n": renamed["h"], "at": attrs(key, {"n": "now called.txt"})}],
    ]
    listing = open_folder(fake).list()
    assert sorted(f.path for f in listing.files) == ["keep.txt", "new.txt", "now called.txt"]
    assert listing.folders == []
