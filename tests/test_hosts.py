from __future__ import annotations

import base64
import hashlib

import pytest
import requests

from trove.errors import AbortRun, PermanentTransferError, ShareError
from trove.model import RemoteFile
from trove.providers import allsync, is_provider_url, mediafire, open_url, yandex


class FakeResponse:
    def __init__(self, status=200, data=None, text="", headers=None):
        self.status_code = status
        self._data = data
        self.text = text
        self.reason = "x"
        self.headers = headers or {}
        self.content = text.encode()

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(str(self.status_code), response=self)

    def close(self):
        pass

    def iter_content(self, chunk_size=1):
        yield b"data"


# -- detection --------------------------------------------------------------


@pytest.mark.parametrize(
    "url,provider",
    [
        ("https://www.mediafire.com/folder/abc123/Some_Name", "mediafire"),
        ("https://www.mediafire.com/file/abc123/name.pdf/file", "mediafire"),
        ("mediafire.com/folder/abc123", "mediafire"),
        ("https://yadi.sk/d/abc123", "yandex"),
        ("https://disk.yandex.com/d/abc123", "yandex"),
        ("https://disk.yandex.ru/i/abc123", "yandex"),
        ("https://cloud.example/s/abc123", "allsync"),
    ],
)
def test_links_go_to_the_right_provider(url, provider):
    assert open_url(url).provider_name == provider


@pytest.mark.parametrize(
    "url",
    ["https://1024terabox.com/s/1abc", "https://www.terabox.com/s/1abc", "https://www.mediafire.com/help", "https://disk.yandex.com/client"],
)
def test_unsupported_links_are_not_claimed(url):
    assert not is_provider_url(url)
    with pytest.raises(ShareError):
        open_url(url)


def test_lookalike_hosts_are_not_claimed():
    assert not mediafire.matches("https://notmediafire.example/folder/x")
    assert not yandex.matches("https://yadi.sk.example/d/x")


def test_allsync_still_accepts_other_hosts():
    assert allsync.matches("https://any.example/s/token")


# -- MediaFire --------------------------------------------------------------


def mf_response(body):
    return FakeResponse(data={"response": {"result": "Success", **body}})


def mf_share(monkeypatch, tree, infos=None):
    share = mediafire.open_share("https://www.mediafire.com/folder/root1/x")

    def request(method, url, params=None, **kwargs):
        if url.endswith("folder/get_info.php"):
            return mf_response({"folder_info": {"name": "My Folder", "created": "2020-09-14 12:26:40"}})
        if url.endswith("file/get_info.php"):
            return mf_response({"file_info": infos})
        key, kind = params["folder_key"], params["content_type"]
        return mf_response({"folder_content": {kind: tree[key].get(kind, []), "more_chunks": "no"}})

    monkeypatch.setattr(share.session, "request", request)
    return share


def test_mediafire_walks_folders(monkeypatch):
    tree = {
        "root1": {
            "files": [{"filename": "a.pdf", "size": "10", "created": "2020-09-14 12:26:40", "hash": "h1", "quickkey": "q1"}],
            "folders": [{"name": "Sub", "folderkey": "sub1", "created": "2020-09-14 12:26:40"}],
        },
        "sub1": {"files": [{"filename": "b.pdf", "size": "20", "created_utc": "2020-09-14T12:26:40Z", "hash": "h2", "quickkey": "q2"}]},
    }
    listing = mf_share(monkeypatch, tree).list()
    assert listing.share_name == "My Folder"
    assert {f.path: (f.size, f.etag, f.key) for f in listing.files} == {"a.pdf": (10, "h1", "q1"), "Sub/b.pdf": (20, "h2", "q2")}
    assert [f.path for f in listing.folders] == ["Sub"]
    assert listing.files[0].mtime == listing.files[1].mtime == 1600086400.0


def test_mediafire_reads_every_chunk(monkeypatch):
    share = mediafire.open_share("https://www.mediafire.com/folder/root1")
    pages = []

    def request(method, url, params=None, **kwargs):
        pages.append(params["chunk"])
        more = "yes" if params["chunk"] == 1 else "no"
        return mf_response({"folder_content": {"files": [{"filename": f"{params['chunk']}.pdf"}], "more_chunks": more}})

    monkeypatch.setattr(share.session, "request", request)
    assert len(share._content("root1", "files")) == 2 and pages == [1, 2]


def test_mediafire_subfolder_listing(monkeypatch):
    tree = {
        "root1": {
            "files": [{"filename": "a.pdf", "size": "1", "quickkey": "q1"}],
            "folders": [{"name": "Sub", "folderkey": "sub1", "created": "2020-09-14 12:26:40"}],
        },
        "sub1": {"files": [{"filename": "b.pdf", "size": "1", "quickkey": "q2"}]},
    }
    listing = mf_share(monkeypatch, tree).list("Sub")
    assert [f.path for f in listing.files] == ["Sub/b.pdf"] and listing.share_name == "My Folder"
    assert listing.root_mtime == 1600086400.0


def test_mediafire_single_file_gets_its_own_folder_name(monkeypatch):
    share = mediafire.open_share("https://www.mediafire.com/file/q9/x.pdf/file")
    monkeypatch.setattr(
        share.session,
        "request",
        lambda m, u, **k: mf_response({"file_info": {"filename": "Book.pdf", "size": "5", "hash": "h"}}),
    )
    listing = share.list()
    assert listing.share_name == "Book" and listing.files[0].key == "q9"


def test_mediafire_duplicate_names_get_stable_suffixes():
    files = [RemoteFile("d/a.pdf", key="key11111xx"), RemoteFile("d/A.pdf", key="key22222xx"), RemoteFile("d/b.pdf", key="k3")]
    mediafire.disambiguate(files)
    assert [f.path for f in files] == ["d/a (key11111).pdf", "d/A (key22222).pdf", "d/b.pdf"]


def test_mediafire_error_marks_dead_links(monkeypatch):
    share = mediafire.open_share("https://www.mediafire.com/folder/dead")
    monkeypatch.setattr(
        share.session,
        "request",
        lambda m, u, **k: FakeResponse(data={"response": {"result": "Error", "message": "Folder not found"}}),
    )
    with pytest.raises(ShareError) as exc:
        share.list()
    assert exc.value.gone is True


@pytest.mark.parametrize(
    "page,expected",
    [
        (f'<a data-scrambled-url="{base64.b64encode(b"https://download1.example/x").decode()}">', "https://download1.example/x"),
        ('<a href="https://download2.example/a?x=1&amp;y=2">', "https://download2.example/a?x=1&y=2"),
        ("<html>removed</html>", None),
    ],
)
def test_mediafire_download_link_extraction(page, expected):
    assert mediafire.extract_download_url(page) == expected


def test_mediafire_removed_file_is_a_permanent_failure(monkeypatch):
    share = mediafire.open_share("https://www.mediafire.com/folder/root1")
    monkeypatch.setattr(share.session, "request", lambda m, u, **k: FakeResponse(text="<html>removed</html>"))
    with pytest.raises(PermanentTransferError):
        share.open_transfer(RemoteFile("a.pdf", key="q1"))


class FakeMediaFireApi:
    """The account endpoints: logs in, hands out tokens, can expire them."""

    def __init__(self, login_ok=True):
        self.login_ok = login_ok
        self.logins = 0
        self.valid = set()
        self.calls = []

    def request(self, method, url, data=None, params=None, **kwargs):
        name = url.rsplit("/api/1.5/", 1)[-1]
        self.calls.append((method, name, dict(data or {})))
        if name == "user/get_session_token.php":
            assert method == "POST" and data["application_id"] == mediafire.APP_ID
            assert data["signature"] == hashlib.sha1(b"me@example.com" + b"s3cret" + mediafire.APP_ID.encode()).hexdigest()
            if not self.login_ok:
                return FakeResponse(data={"response": {"result": "Error", "error": 129, "message": "invalid"}})
            self.logins += 1
            token = f"tok{self.logins}"
            self.valid.add(token)
            return mf_response({"session_token": token})
        if data and "session_token" in data and data["session_token"] not in self.valid:
            return FakeResponse(data={"response": {"result": "Error", "error": 105, "message": "expired"}})
        if name == "file/get_links.php":
            return mf_response({"links": [{"quickkey": data["quick_key"], "direct_download": f"https://download5.example/{data['quick_key']}"}]})
        if name == "user/get_info.php":
            return mf_response({"user_info": {"premium": "yes"}})
        raise AssertionError(name)


def account_share(monkeypatch, api):
    share = mediafire.MediaFireShare("folder", "root1", credentials=lambda: ("me@example.com", "s3cret"))
    monkeypatch.setattr(share.session, "request", api.request)
    fetched = {}

    def get(url, **kwargs):
        fetched.update(url=url, **kwargs)
        return FakeResponse(206)

    monkeypatch.setattr(share.session, "get", get)
    return share, fetched


def test_mediafire_account_downloads_by_direct_link(monkeypatch):
    api = FakeMediaFireApi()
    share, fetched = account_share(monkeypatch, api)
    transfer = share.open_transfer(RemoteFile("a.pdf", key="q1"), 7)
    assert fetched["url"] == "https://download5.example/q1" and fetched["headers"]["Range"] == "bytes=7-"
    assert transfer.offset == 7
    share.open_transfer(RemoteFile("b.pdf", key="q2"))
    assert api.logins == 1, "one login serves every file of the share"
    # The password and token travel in POST bodies, never in a URL an error could quote.
    assert all(method == "POST" for method, _, _ in api.calls)


def test_mediafire_account_logs_in_again_when_the_token_expires(monkeypatch):
    api = FakeMediaFireApi()
    share, fetched = account_share(monkeypatch, api)
    share.open_transfer(RemoteFile("a.pdf", key="q1"))
    api.valid.clear()  # the session times out
    share.open_transfer(RemoteFile("b.pdf", key="q2"))
    assert api.logins == 2 and fetched["url"] == "https://download5.example/q2"


def test_mediafire_refused_login_stops_the_run_without_the_password(monkeypatch):
    share, _ = account_share(monkeypatch, FakeMediaFireApi(login_ok=False))
    with pytest.raises(AbortRun) as exc:
        share.open_transfer(RemoteFile("a.pdf", key="q1"))
    assert "s3cret" not in str(exc.value) and "me@example.com" not in str(exc.value)


def test_mediafire_listing_never_logs_in(monkeypatch):
    looked_up = []
    share = mediafire.MediaFireShare("folder", "root1", credentials=lambda: looked_up.append(1) or ("e", "p"))
    tree = {"root1": {"files": [{"filename": "a.pdf", "size": "3", "quickkey": "q1"}]}}

    def request(method, url, params=None, **kwargs):
        assert method == "GET" and "session_token" not in (params or {})
        if url.endswith("folder/get_info.php"):
            return mf_response({"folder_info": {"name": "My Folder"}})
        key, kind = params["folder_key"], params["content_type"]
        return mf_response({"folder_content": {kind: tree[key].get(kind, []), "more_chunks": "no"}})

    monkeypatch.setattr(share.session, "request", request)
    share.list()
    assert not looked_up, "a dry run or an all-skip run must not touch the account"


def test_mediafire_credentials_come_from_the_environment_first(monkeypatch):
    monkeypatch.undo()  # the autouse stub replaced find_credentials; test the real one
    monkeypatch.setenv(mediafire.EMAIL_ENV, "me@example.com")
    monkeypatch.setenv(mediafire.PASSWORD_ENV, "s3cret")
    assert mediafire.find_credentials() == ("me@example.com", "s3cret")


def test_mediafire_transfer_fetches_page_then_resumes(monkeypatch):
    share = mediafire.open_share("https://www.mediafire.com/folder/root1")
    monkeypatch.setattr(share.session, "request", lambda m, u, **k: FakeResponse(text='<a href="https://download9.example/f">'))
    seen = {}

    def get(url, **kwargs):
        seen.update(url=url, **kwargs)
        return FakeResponse(206)

    monkeypatch.setattr(share.session, "get", get)
    transfer = share.open_transfer(RemoteFile("a.pdf", key="q1"), 7)
    assert seen["url"] == "https://download9.example/f" and seen["headers"]["Range"] == "bytes=7-"
    assert transfer.offset == 7


# -- Yandex -----------------------------------------------------------------


def yd_item(path, kind="file", size=10, sha="s", modified="2020-09-14T12:26:40+00:00"):
    return {"path": path, "name": path.rsplit("/", 1)[-1], "type": kind, "size": size, "sha256": sha, "modified": modified}


def yd_share(monkeypatch, tree):
    share = yandex.open_share("https://yadi.sk/d/abc?x=1#frag")

    def request(method, url, params=None, **kwargs):
        if url.endswith("/download"):
            return FakeResponse(data={"href": "https://downloader.example/f", "path_seen": params.get("path")})
        return FakeResponse(data=tree(params.get("path", "/"), params.get("offset", 0)))

    monkeypatch.setattr(share.session, "request", request)
    return share


def test_yandex_public_key_drops_query_and_fragment():
    assert yandex.open_share("https://yadi.sk/d/abc?x=1#frag").public_key == "https://yadi.sk/d/abc"


def test_yandex_walks_folders_and_pages(monkeypatch):
    many = [yd_item(f"/f{i}.pdf") for i in range(200)]

    def tree(path, offset):
        if path == "/":
            items = many if offset == 0 else [yd_item("/last.pdf"), yd_item("/Sub", "dir")]
            return {"name": "My Share", "type": "dir", "modified": "2020-09-14T12:26:40+00:00", "_embedded": {"items": items}}
        return {"name": "Sub", "type": "dir", "_embedded": {"items": [yd_item("/Sub/in.pdf", sha="deep")]}}

    listing = yd_share(monkeypatch, tree).list()
    assert listing.share_name == "My Share" and len(listing.files) == 202
    deep = next(f for f in listing.files if f.path == "Sub/in.pdf")
    assert deep.etag == "deep" and deep.mtime == 1600086400.0 and deep.size == 10
    assert [f.path for f in listing.folders] == ["Sub"]


def test_yandex_subfolder_keeps_the_share_name(monkeypatch):
    def tree(path, offset):
        if path == "/":
            return {"name": "My Share", "type": "dir", "_embedded": {"items": []}}
        return {"name": "Sub", "type": "dir", "modified": "2020-09-14T12:26:40+00:00", "_embedded": {"items": [yd_item("/Sub/a.pdf")]}}

    listing = yd_share(monkeypatch, tree).list("Sub")
    assert listing.share_name == "My Share" and [f.path for f in listing.files] == ["Sub/a.pdf"]


def test_yandex_single_file_share_has_no_path_on_download(monkeypatch):
    def tree(path, offset):
        return {"name": "Book.pdf", "type": "file", "size": 5, "sha256": "h", "modified": "2020-09-14T12:26:40+00:00"}

    share = yd_share(monkeypatch, tree)
    listing = share.list()
    assert listing.share_name == "Book" and listing.files[0].path == "Book.pdf"
    seen = {}
    real = share.session.request

    def spy(method, url, params=None, **kwargs):
        seen.update(params or {})
        return real(method, url, params=params, **kwargs)

    monkeypatch.setattr(share.session, "request", spy)
    monkeypatch.setattr(share.session, "get", lambda url, **k: FakeResponse(200))
    share.open_transfer(listing.files[0])
    assert "path" not in seen


def test_yandex_folder_file_download_uses_its_path(monkeypatch):
    share = yandex.open_share("https://yadi.sk/d/abc")
    seen = {}

    def request(method, url, params=None, **kwargs):
        seen.update(params)
        return FakeResponse(data={"href": "https://downloader.example/f"})

    monkeypatch.setattr(share.session, "request", request)
    monkeypatch.setattr(share.session, "get", lambda url, **k: FakeResponse(206))
    transfer = share.open_transfer(RemoteFile("Sub/a.pdf"), 3)
    assert seen["path"] == "/Sub/a.pdf" and transfer.offset == 3


def test_yandex_download_limit_is_a_permanent_failure(monkeypatch):
    share = yandex.open_share("https://yadi.sk/d/abc")
    monkeypatch.setattr(
        share.session,
        "request",
        lambda m, u, **k: FakeResponse(403, data={"description": "limit exceeded"}),
    )
    with pytest.raises(PermanentTransferError) as exc:
        share.open_transfer(RemoteFile("a.pdf"))
    assert "limit exceeded" in str(exc.value)


def test_yandex_dead_share_reports_gone(monkeypatch):
    from trove.providers import is_gone_error

    share = yandex.open_share("https://yadi.sk/d/abc")
    monkeypatch.setattr(share.session, "request", lambda m, u, **k: FakeResponse(404, data={"error": "NotFoundError"}))
    with pytest.raises(requests.exceptions.HTTPError) as exc:
        share.list()
    assert is_gone_error(exc.value)
