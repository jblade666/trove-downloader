"""workupload, without a browser: links, page parsing and the checked transfer."""

from __future__ import annotations

import hashlib

import pytest

from conftest import MemFS
from trove.engine import SyncOptions, sync_listing
from trove.errors import CorruptDownload, ShareError
from trove.model import Listing, RemoteFile
from trove.providers import open_url, workupload

PAGE = """Home | Login
Some Book.pdf (6.28 MB)
Download the file Some Book.pdf now.
Filename:\tSome Book.pdf
Filesize:\t6588091 (Byte)
Checksum:\tC1AC5BD99C19E4889F00EB7012C3B5440F39A652F183311D3E42BD86DDD92BB4 (SHA256)
"""


def test_links():
    assert open_url("https://workupload.com/file/AbC12dEf34").provider_name == "workupload"
    assert workupload.matches("workupload.com/file/AbC12dEf34/")
    assert not workupload.matches("https://workupload.com/report/AbC12dEf34")
    assert not workupload.matches("https://workupload.example/file/AbC12dEf34")


def test_page_parsing():
    info = workupload.parse_page(PAGE)
    assert info == {"name": "Some Book.pdf", "size": 6588091,
                    "sha256": "c1ac5bd99c19e4889f00eb7012c3b5440f39a652f183311d3e42bd86ddd92bb4"}
    assert workupload.parse_page("nothing here") == {"name": None, "size": None, "sha256": None}


def test_listing_from_the_page(monkeypatch):
    share = workupload.open_share("https://workupload.com/file/AbC12dEf34")
    monkeypatch.setattr(share, "_open_file_page", lambda: PAGE)
    listing = share.list()
    (file,) = listing.files
    assert (file.path, file.size, file.etag) == ("Some Book.pdf", 6588091, workupload.parse_page(PAGE)["sha256"])
    assert listing.share_name == "Some Book" and share.verify_kind == "sha256"


def test_a_page_with_no_file_is_gone(monkeypatch):
    share = workupload.open_share("https://workupload.com/file/AbC12dEf34")
    monkeypatch.setattr(share, "_open_file_page", lambda: "File not found")
    with pytest.raises(ShareError) as e:
        share.list()
    assert e.value.gone


class FakeDownload:
    def delete(self):
        pass


def spooling(tmp_path, data: bytes):
    share = workupload.open_share("https://workupload.com/file/AbC12dEf34")

    def download(file):
        path = tmp_path / "spool"
        path.write_bytes(data)
        share._spooled[file.key] = (str(path), FakeDownload())
        return str(path)

    share._download = download
    return share


def test_a_download_matching_its_hash_is_kept(tmp_path):
    fs = MemFS()
    data = b"pdf bytes"
    share = spooling(tmp_path, data)
    file = RemoteFile("a.pdf", len(data), None, hashlib.sha256(data).hexdigest().upper(), key="k")
    result = sync_listing(Listing([file], [], share_name="a"), share, fs, "/out", SyncOptions(no_orphans=True))
    assert result.downloaded == 1 and fs.files["/out/a.pdf"][0] == data and share._spooled == {}


def test_a_download_not_matching_its_hash_is_discarded(tmp_path):
    share = spooling(tmp_path, b"wrong bytes")
    file = RemoteFile("a.pdf", 11, None, hashlib.sha256(b"right bytes").hexdigest(), key="k")
    with pytest.raises(CorruptDownload):
        share.open_transfer(file)
    assert share._spooled == {}


def test_downloads_need_a_display(monkeypatch):
    monkeypatch.setattr("trove.browser.sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with pytest.raises(ShareError, match="headless server"):
        workupload.open_share("https://workupload.com/file/AbC12dEf34")._browser_page()
