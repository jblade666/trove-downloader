"""ufile, without a browser: link parsing, page text parsing and the spooled transfer."""

from __future__ import annotations

from datetime import datetime

import pytest

from conftest import MemFS
from trove.engine import SyncOptions, sync_listing
from trove.errors import ShareError
from trove.model import Listing, RemoteFile
from trove.providers import open_url, ufile


@pytest.mark.parametrize(
    "url,kind,slug",
    [("https://ufile.io/f/abc12", "folder", "abc12"), ("ufile.io/f/abc12/", "folder", "abc12"),
     ("https://ufile.io/a1b2c3d4", "file", "a1b2c3d4")],
)
def test_links(url, kind, slug):
    share = open_url(url)
    assert (share.provider_name, share.kind, share.slug) == ("ufile", kind, slug)


@pytest.mark.parametrize(
    "url", ["https://ufile.io/terms", "https://ufile.io/pricing", "https://ufile.io/business", "https://ufile.io/register",
            "https://ufile.example/f/abc12"],
)
def test_site_pages_are_not_files(url):
    assert not ufile.matches(url)


def test_page_text_parsing():
    assert ufile.parse_created("File created: 4/13/2026, 1:13:26 PM") == datetime(2026, 4, 13, 13, 13, 26).timestamp()
    assert ufile.parse_created("File created: 2026-04-13 13:13") == datetime(2026, 4, 13, 13, 13).timestamp()
    assert ufile.parse_created("nothing") is None


def test_downloads_need_a_display_on_linux(monkeypatch):
    monkeypatch.setattr("trove.browser.sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert not ufile.has_display()
    share = ufile.open_share("https://ufile.io/f/abc12")
    with pytest.raises(ShareError, match="headless server"):
        share._browser_page()


class FakeDownload:
    deleted = 0

    def delete(self):
        FakeDownload.deleted += 1


def spooling_share(tmp_path, data: bytes, created=1_600_000_000.0):
    share = ufile.open_share("https://ufile.io/f/abc12")
    calls = []

    def download(file):
        calls.append(file.key)
        path = tmp_path / f"spool{len(calls)}"
        path.write_bytes(data)
        file.mtime = created
        share._spooled[file.key] = (str(path), FakeDownload())
        return str(path)

    share._download = download
    return share, calls


def test_a_download_is_streamed_from_the_spooled_file_and_dated(tmp_path):
    fs = MemFS()
    share, calls = spooling_share(tmp_path, b"x" * 3000)
    listing = Listing([RemoteFile("a.pdf", None, None, key="https://ufile.io/a1b2c3d4")], [], share_name="Folder")
    result = sync_listing(listing, share, fs, "/out", SyncOptions(no_orphans=True))
    assert result.downloaded == 1 and result.errors == 0 and calls == ["https://ufile.io/a1b2c3d4"]
    data, mtime = fs.files["/out/a.pdf"]
    assert data == b"x" * 3000 and mtime == 1_600_000_000.0
    assert share._spooled == {}  # read through, so the temp copy is gone


def test_a_retry_reuses_the_spooled_file(tmp_path):
    share, calls = spooling_share(tmp_path, b"0123456789")
    file = RemoteFile("a.pdf", key="k")
    first = share.open_transfer(file)
    assert next(first.chunks) == b"0123456789"  # not read through: the copy stays
    again = share.open_transfer(file, 4)
    assert again.offset == 4 and b"".join(again.chunks) == b"456789"
    assert calls == ["k"] and file.size == 10


def test_an_existing_file_is_kept_whatever_the_rounded_size(tmp_path):
    fs = MemFS()
    fs.makedirs("/out")
    with fs.open("/out/a.pdf", "wb") as fp:
        fp.write(b"abc")
    share, calls = spooling_share(tmp_path, b"new")
    listing = Listing([RemoteFile("a.pdf", None, None, key="k")], [], share_name="Folder")
    result = sync_listing(listing, share, fs, "/out", SyncOptions())
    assert result.skipped == 1 and calls == []


def test_close_releases_spooled_downloads(tmp_path):
    share, _calls = spooling_share(tmp_path, b"data")
    share.open_transfer(RemoteFile("a.pdf", key="k"))
    before = FakeDownload.deleted
    share.close()
    assert FakeDownload.deleted == before + 1 and share._spooled == {}


def test_a_password_is_refused():
    with pytest.raises(ShareError):
        ufile.open_share("https://ufile.io/f/abc12", "pw")


def test_a_short_spooled_copy_is_caught_by_the_size_learned_at_transfer(tmp_path, monkeypatch):
    fs = MemFS()
    share, _calls = spooling_share(tmp_path, b"x" * 100)
    original = share._read

    def short_read(key, path, start):
        yield from list(original(key, path, start))[:0] or [b"x" * 40]

    monkeypatch.setattr(share, "_read", short_read)
    listing = Listing([RemoteFile("a.pdf", None, None, key="k")], [], share_name="Folder")
    result = sync_listing(listing, share, fs, "/out", SyncOptions(no_orphans=True))
    assert result.errors == 1 and "/out/a.pdf" not in fs.files
