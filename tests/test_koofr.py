from __future__ import annotations

import hashlib

import pytest
import requests

from conftest import FakeShare, make_listing
from trove.engine import SyncOptions, sync_listing
from trove.errors import PermanentTransferError, ShareError
from trove.http import resolve_shortener
from trove.model import RemoteFile
from trove.providers import _safe_url, is_gone_error, koofr, open_url, select_share

LINK = "c0ffee00-1234-4abc-8def-001122334455"
URL = f"https://app.koofr.net/links/{LINK}"
PASSWORD = "hunter2 & more"


class FakeResponse:
    def __init__(self, status=200, data=None, content=b"", reason="OK", url="https://app.koofr.net/x?password=hunter2"):
        self.status_code, self.data, self.content, self.reason, self.url = status, data, content, reason, url
        self.headers = {}

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} for url: {self.url}", response=self)

    def close(self):
        pass

    def iter_content(self, chunk_size=1):
        yield self.content


TREE = {
    "/": [
        {"name": "Some: Dir", "type": "dir", "modified": 1_600_000_000_500},
        {"name": "top.pdf", "type": "file", "modified": 1_600_000_000_000, "size": 10, "hash": "abc"},
    ],
    "/Some: Dir": [
        {"name": "in&ner.pdf", "type": "file", "modified": 1_600_000_001_000, "size": 20, "hash": "def"},
    ],
}


def fake_server(share, calls=None, info=None):
    def request(method, url, **kwargs):
        if calls is not None:
            calls.append((url, kwargs))
        if url.endswith("/bundle"):
            return FakeResponse(data={"files": TREE[kwargs["params"]["path"]]})
        return FakeResponse(data=info or {"name": "My Link", "isOnline": True})

    share.session.request = request


def test_link_parsing():
    assert koofr.matches(URL)
    assert koofr.matches(f"app.koofr.net/links/{LINK}")
    assert koofr.matches(f"{URL}?password=x")
    assert koofr.matches(LINK)
    assert koofr.open_share(LINK.upper()).link_id == LINK
    assert not koofr.matches("https://k00.fr/abc")
    assert not koofr.matches(f"https://elsewhere.example/links/{LINK}")
    assert not koofr.matches("https://notkoofr.net/links/" + LINK)
    assert not koofr.matches("https://app.koofr.net/links/short")


def test_registry_sends_koofr_links_to_koofr():
    assert open_url(URL).provider_name == "koofr"


def test_a_bare_id_is_not_fetched_as_a_hostname():
    assert resolve_shortener(LINK, koofr.matches, session=object()) == LINK


def test_listing_walks_the_tree_and_keeps_remote_names():
    share = koofr.open_share(URL)
    fake_server(share)
    listing = share.list()
    assert listing.share_name == "My Link"
    assert {f.path: (f.size, f.etag, f.mtime) for f in listing.files} == {
        "top.pdf": (10, "abc", 1_600_000_000.0),
        "Some: Dir/in&ner.pdf": (20, "def", 1_600_000_001.0),
    }
    assert [(f.path, f.mtime) for f in listing.folders] == [("Some: Dir", 1_600_000_000.5)]


def test_subfolder_listing_keeps_the_prefix_and_the_share_name():
    share = koofr.open_share(URL)
    fake_server(share)
    listing = share.list("/Some: Dir/")
    assert listing.share_name == "My Link"
    assert [f.path for f in listing.files] == ["Some: Dir/in&ner.pdf"]


def test_every_request_carries_the_password_and_the_listing_referer():
    share = koofr.open_share(URL, PASSWORD)
    calls = []
    fake_server(share, calls)
    share.list()
    assert len(calls) == 3
    assert all(kw["params"]["password"] == PASSWORD for _, kw in calls)
    assert all(kw["headers"]["Referer"] == "https://app.koofr.net/" for _, kw in calls)


def test_no_password_parameter_without_a_password():
    share = koofr.open_share(URL)
    calls = []
    fake_server(share, calls)
    share.list()
    assert all("password" not in (kw["params"] or {}) for _, kw in calls)


def test_download_sends_the_link_referer_and_the_password(monkeypatch):
    share = koofr.open_share(URL, PASSWORD)
    seen = {}

    def get(url, **kwargs):
        seen.update(url=url, **kwargs)
        return FakeResponse(206, content=b"rest")

    monkeypatch.setattr(share.session, "get", get)
    transfer = share.open_transfer(RemoteFile("A Dir/f&g.pdf"), 4)
    assert seen["url"] == f"https://app.koofr.net/content/links/{LINK}/files/get"
    assert seen["params"] == {"path": "/A Dir/f&g.pdf", "password": PASSWORD}
    assert seen["headers"]["Referer"] == URL
    assert seen["headers"]["Range"] == "bytes=4-"
    assert transfer.offset == 4 and b"".join(transfer.chunks) == b"rest"


@pytest.mark.parametrize("password,expected", [("", "needs a password"), ("nope", "wrong password")])
def test_401_is_a_clear_error_and_is_not_retried(password, expected):
    share = koofr.open_share(URL, password)
    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        return FakeResponse(401)

    share.session.request = request
    with pytest.raises(ShareError, match=expected):
        share.list()
    assert len(calls) == 1


def test_offline_link_counts_as_gone():
    share = koofr.open_share(URL)
    fake_server(share, info={"name": "x", "isOnline": False})
    with pytest.raises(ShareError) as e:
        share.list()
    assert e.value.gone


def test_probe_treats_a_protected_link_as_usable_but_a_dead_one_not():
    share = koofr.open_share(URL)
    share.session.request = lambda *a, **k: FakeResponse(401)
    share.probe()

    share.session.request = lambda *a, **k: FakeResponse(404)
    with pytest.raises(requests.exceptions.HTTPError) as e:
        share.probe()
    assert is_gone_error(e.value)


def test_select_share_falls_through_a_dead_link():
    dead = f"https://app.koofr.net/links/{'0' * 8}-0000-4000-8000-{'0' * 12}"

    def request(self, method, url, **kwargs):
        return FakeResponse(404 if "00000000" in url else 200, data={"isOnline": True})

    original = requests.Session.request
    requests.Session.request = request
    try:
        assert select_share([dead, URL]).link_id == LINK
    finally:
        requests.Session.request = original


def test_listing_errors_never_show_the_password():
    share = koofr.open_share(URL, "hunter2")

    def request(method, url, **kwargs):
        raise requests.exceptions.ConnectionError(f"Max retries exceeded with url: /x?path=%2F&password=hunter2")

    share.session.request = request
    with pytest.raises(requests.exceptions.ConnectionError) as e:
        share.list()
    assert "hunter2" not in str(e.value) and "password=***" in str(e.value)


def test_a_404_stays_recognizable_as_gone_after_scrubbing():
    share = koofr.open_share(URL, "hunter2")
    share.session.request = lambda *a, **k: FakeResponse(404)
    with pytest.raises(requests.exceptions.HTTPError) as e:
        share.list()
    assert "hunter2" not in str(e.value) and is_gone_error(e.value)


def test_download_errors_never_show_the_password(monkeypatch):
    share = koofr.open_share(URL, "hunter2")
    monkeypatch.setattr(share.session, "get", lambda url, **k: FakeResponse(500))
    with pytest.raises(OSError) as e:
        share.open_transfer(RemoteFile("a.pdf"))
    assert "hunter2" not in str(e.value)

    def dropped(url, **k):
        response = FakeResponse()

        def iter_content(chunk_size=1):
            raise requests.exceptions.ConnectionError("lost /x?password=hunter2")
            yield b""

        response.iter_content = iter_content
        return response

    monkeypatch.setattr(share.session, "get", dropped)
    with pytest.raises(OSError) as e:
        b"".join(share.open_transfer(RemoteFile("a.pdf")).chunks)
    assert "hunter2" not in str(e.value)


def test_a_refused_download_is_permanent(monkeypatch):
    share = koofr.open_share(URL, "hunter2")
    monkeypatch.setattr(share.session, "get", lambda url, **k: FakeResponse(403, reason="Forbidden"))
    with pytest.raises(PermanentTransferError) as e:
        share.open_transfer(RemoteFile("a.pdf"))
    assert "hunter2" not in str(e.value)


def test_describe_and_links_never_print_the_password():
    assert "hunter2" not in " ".join(koofr.open_share(URL, "hunter2").describe())
    assert "hunter2" not in _safe_url(f"{URL}?password=hunter2&x=1")


def test_shares_keep_separate_state():
    first, second = koofr.open_share(URL, "one"), koofr.open_share(URL)
    assert first.session is not second.session and second.password == ""


def test_colons_keep_the_dash_form():
    assert koofr.KoofrShare.colon == "dash"


# -- --verify with MD5 -------------------------------------------------------


class Md5Share(FakeShare):
    verify_kind = "md5"
    manifest_name = None


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def listing_for(share, hashes):
    return make_listing(share, etags=hashes)


def test_verify_keeps_a_file_whose_hash_matches(dest):
    fs, out = dest
    share = Md5Share({"a.pdf": b"contents"})
    sync_listing(listing_for(share, {"a.pdf": md5(b"contents")}), share, fs, out, SyncOptions())
    share.transfers.clear()
    result = sync_listing(listing_for(share, {"a.pdf": md5(b"contents").upper()}), share, fs, out, SyncOptions(verify=True))
    assert share.transfers == [] and result.skipped == 1 and result.errors == 0


def test_verify_replaces_a_file_with_the_same_size_but_different_content(dest):
    fs, out = dest
    share = Md5Share({"a.pdf": b"old-data"})
    sync_listing(listing_for(share, {"a.pdf": md5(b"old-data")}), share, fs, out, SyncOptions())
    share.contents["a.pdf"] = b"new-data"
    result = sync_listing(listing_for(share, {"a.pdf": md5(b"new-data")}), share, fs, out, SyncOptions(verify=True))
    assert result.reverified == 1 and result.downloaded == 1
    with fs.open(fs.join(out, "a.pdf"), "rb") as fp:
        assert fp.read() == b"new-data"


def test_verify_in_a_dry_run_reports_without_writing(dest):
    fs, out = dest
    share = Md5Share({"a.pdf": b"old-data"})
    sync_listing(listing_for(share, {"a.pdf": md5(b"old-data")}), share, fs, out, SyncOptions())
    share.contents["a.pdf"] = b"new-data"
    share.transfers.clear()
    result = sync_listing(listing_for(share, {"a.pdf": md5(b"new-data")}), share, fs, out, SyncOptions(verify=True, dry_run=True))
    assert result.would_download == 1 and share.transfers == []
    with fs.open(fs.join(out, "a.pdf"), "rb") as fp:
        assert fp.read() == b"old-data"


def test_without_verify_nothing_is_read_back(dest, monkeypatch):
    fs, out = dest
    share = Md5Share({"a.pdf": b"contents"})
    sync_listing(listing_for(share, {"a.pdf": md5(b"contents")}), share, fs, out, SyncOptions())
    monkeypatch.setattr("trove.engine.file_hash", lambda *a: pytest.fail("read back without --verify"))
    result = sync_listing(listing_for(share, {"a.pdf": "something-else"}), share, fs, out, SyncOptions())
    assert result.skipped == 1


def test_a_bad_download_is_discarded_and_the_older_copy_stays(dest):
    fs, out = dest
    share = Md5Share({"a.pdf": b"old-data"})
    sync_listing(listing_for(share, {"a.pdf": md5(b"old-data")}), share, fs, out, SyncOptions())
    share.contents["a.pdf"] = b"corrupt!"
    share.transfers.clear()
    result = sync_listing(listing_for(share, {"a.pdf": md5(b"new-data")}), share, fs, out, SyncOptions(verify=True))
    assert result.errors == 1 and result.downloaded == 0 and len(share.transfers) == 1
    with fs.open(fs.join(out, "a.pdf"), "rb") as fp:
        assert fp.read() == b"old-data"
    assert fs.stat(fs.join(out, "a.pdf.part")) is None


def test_a_good_fresh_download_is_checked_while_streaming(dest):
    fs, out = dest
    share = Md5Share({"a.pdf": b"contents"})
    result = sync_listing(listing_for(share, {"a.pdf": md5(b"contents")}), share, fs, out, SyncOptions(verify=True))
    assert result.downloaded == 1 and result.errors == 0


def test_a_file_with_no_hash_is_only_size_checked(dest):
    fs, out = dest
    share = Md5Share({"a.pdf": b"contents"})
    sync_listing(listing_for(share, {}), share, fs, out, SyncOptions())
    share.transfers.clear()
    result = sync_listing(listing_for(share, {}), share, fs, out, SyncOptions(verify=True))
    assert result.skipped == 1 and share.transfers == []


def test_seed_manifest_is_refused_for_hash_shares(dest):
    fs, out = dest
    share = Md5Share({"a.pdf": b"contents"})
    with pytest.raises(ShareError):
        sync_listing(listing_for(share, {}), share, fs, out, SyncOptions(seed_manifest=True))
