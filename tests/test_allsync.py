from __future__ import annotations

import pytest
import requests

from trove import http as trove_http
from trove.errors import PermanentTransferError, StalePartial
from trove.http import open_http_transfer, resolve_shortener
from trove.model import RemoteFile
from trove.providers import PROVIDERS, allsync, select_share


def propfind_xml(*responses):
    return ('<d:multistatus xmlns:d="DAV:">' + "".join(responses) + "</d:multistatus>").encode()


def entry(path, name=None, is_dir=False, size=None, etag=None, date="Mon, 14 Sep 2020 12:26:40 GMT"):
    href = f"/public.php/webdav/{path}" + ("/" if is_dir and path else "")
    props = f"<d:getlastmodified>{date}</d:getlastmodified>"
    props += "<d:resourcetype><d:collection/></d:resourcetype>" if is_dir else "<d:resourcetype/>"
    if name:
        props += f"<d:displayname>{name}</d:displayname>"
    if size is not None:
        props += f"<d:getcontentlength>{size}</d:getcontentlength>"
    if etag:
        props += f'<d:getetag>"{etag}"</d:getetag>'
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


TREE = {
    "": propfind_xml(
        entry("", name="My Share", is_dir=True),
        entry("Some%20Dir", is_dir=True),
        entry("top.pdf", size=10, etag="e1"),
    ),
    "Some Dir": propfind_xml(
        entry("Some%20Dir", is_dir=True),
        entry("Some%20Dir/inner%26more.pdf", size=20, etag="e2"),
    ),
}


class FakeResponse:
    def __init__(self, status=200, content=b"", headers=None, reason="OK"):
        self.status_code, self.content, self.headers, self.reason = status, content, headers or {}, reason
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size=1):
        yield self.content


@pytest.fixture
def share(monkeypatch):
    s = allsync.open_share("https://cloud.example/s/tok123", "pw")

    def request(method, url, **kwargs):
        assert method == "PROPFIND" and kwargs["auth"] == ("tok123", "pw")
        assert kwargs["data"], "a bodyless PROPFIND can return 500"
        rel = requests.utils.unquote(url.split("/public.php/webdav/")[1])
        return FakeResponse(content=TREE[rel])

    monkeypatch.setattr(s.session, "request", request)
    return s


def test_link_parsing():
    assert allsync.matches("https://cloud.example/s/abc")
    assert allsync.matches("cloud.example/s/abc/download")
    assert not allsync.matches("https://short.example/abc")
    share = allsync.open_share("https://cloud.example/s/abc")
    assert (share.host, share.token) == ("cloud.example", "abc")


def test_listing_walks_the_tree(share):
    listing = share.list()
    assert listing.share_name == "My Share"
    assert {f.path: (f.size, f.etag) for f in listing.files} == {
        "top.pdf": (10, "e1"),
        "Some Dir/inner&more.pdf": (20, "e2"),
    }
    assert [f.path for f in listing.folders] == ["Some Dir"]
    assert listing.files[0].mtime == 1600086400.0


def test_subfolder_listing_still_knows_the_share_name(share):
    listing = share.list("Some Dir")
    assert listing.share_name == "My Share"
    assert [f.path for f in listing.files] == ["Some Dir/inner&more.pdf"]


def test_describe_never_prints_the_password():
    share = allsync.open_share("https://cloud.example/s/abc", "hunter2")
    assert "hunter2" not in " ".join(share.describe())
    assert "hunter2" not in repr(share.__dict__.get("host"))


def test_shares_keep_separate_state():
    first = allsync.open_share("https://cloud.example/s/a", "one")
    second = allsync.open_share("https://cloud.example/s/b", "")
    assert first.session is not second.session and second.password == ""


def test_open_transfer_quotes_the_path(share, monkeypatch):
    seen = {}

    def get(url, **kwargs):
        seen.update(url=url, **kwargs)
        return FakeResponse(206, b"rest")

    monkeypatch.setattr(share.session, "get", get)
    transfer = share.open_transfer(RemoteFile("A Dir/f&g.pdf"), 4)
    assert seen["url"].endswith("/public.php/webdav/A%20Dir/f%26g.pdf")
    assert seen["headers"]["Range"] == "bytes=4-" and transfer.offset == 4
    assert b"".join(transfer.chunks) == b"rest"


class StubSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.responses.pop(0)

    def request(self, method, url, **kwargs):
        self.calls.append(url)
        return self.responses.pop(0)


def test_range_ignored_by_server_restarts_from_zero():
    transfer = open_http_transfer(StubSession(FakeResponse(200, b"all")), "http://x", 10)
    assert transfer.offset == 0


def test_rejected_offset_is_a_stale_partial():
    with pytest.raises(StalePartial):
        open_http_transfer(StubSession(FakeResponse(416)), "http://x", 10)


def test_404_is_permanent_and_500_is_retryable():
    with pytest.raises(PermanentTransferError):
        open_http_transfer(StubSession(FakeResponse(404)), "http://x")
    with pytest.raises(OSError):
        open_http_transfer(StubSession(FakeResponse(500)), "http://x")


def test_request_retry_gives_up_fast_on_4xx():
    session = StubSession(FakeResponse(401), FakeResponse(200))
    with pytest.raises(requests.exceptions.HTTPError):
        trove_http.request_with_retry(session, "GET", "http://x")
    assert len(session.calls) == 1


def test_request_retry_recovers_from_5xx():
    session = StubSession(FakeResponse(500), FakeResponse(200, b"ok"))
    assert trove_http.request_with_retry(session, "GET", "http://x").content == b"ok"


def test_shortener_stops_before_fetching_the_target_and_keeps_the_fragment():
    session = StubSession(
        FakeResponse(301, headers={"Location": "https://next.example/x"}),
        FakeResponse(303, headers={"Location": "https://final.example/s/tok#secret"}),
    )
    final = resolve_shortener("https://short.example/a", lambda u: "/s/" in u, session=session)
    assert final == "https://final.example/s/tok#secret"
    assert session.calls == ["https://short.example/a", "https://next.example/x"]


def test_dead_shortener_raises():
    session = StubSession(FakeResponse(200))
    with pytest.raises(ValueError):
        resolve_shortener("https://short.example/a", lambda u: False, session=session)


def test_select_share_falls_back_and_warns_when_stale(monkeypatch, capsys):
    calls = []

    def probe(self):
        calls.append(self.token)
        if self.token == "dead":
            raise OSError("gone")

    monkeypatch.setattr(allsync.AllsyncShare, "probe", probe)
    share = select_share(["https://c.example/s/dead", "https://c.example/s/live"], "")
    assert share.token == "live" and calls == ["dead"]

    calls.clear()
    share = select_share(["https://c.example/s/new", "https://c.example/s/old"], "")
    assert share.token == "new" and "different share" in capsys.readouterr().out


def test_provider_registry_recognizes_allsync():
    assert "allsync" in PROVIDERS
