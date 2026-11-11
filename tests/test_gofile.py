"""gofile, against a fake API. All data is made up here."""

from __future__ import annotations

import hashlib
import json
import time

import pytest
import requests

from trove.errors import AbortRun, ShareError
from trove.providers import gofile, gofile_wt, is_gone_error, open_url

TOKEN = "T" * 32


class Response:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self.data, self.text = status, data, text

    def json(self):
        if self.data is None:
            raise ValueError("no json")
        return self.data


def ok(data):
    return Response(200, {"status": "ok", "data": data})


def node_file(name, fid, size=3, created=1_600_000_000):
    return {"type": "file", "name": name, "id": fid, "size": size, "createTime": created,
            "link": f"https://store.example/download/{fid}/{name}"}


def node_folder(name, children=None, code=None, created=1_600_000_500, **extra):
    node = {"type": "folder", "name": name, "createTime": created, **extra}
    if children is not None:
        node["children"] = {c.get("id") or c["name"]: c for c in children}
    if code:
        node["code"] = code
    return node


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(gofile, "TOKEN_CACHE", tmp_path / "token.json")
    monkeypatch.setattr(gofile, "firefox_token", lambda: None)
    monkeypatch.setattr(gofile, "_salt_checked", False)
    monkeypatch.setattr(gofile, "SUBFOLDER_DELAY", 0)
    monkeypatch.setattr(gofile_wt, "current_salt", lambda: "salt")
    gofile._save_token(TOKEN)


def install(share, contents, calls=None):
    """contents: content id -> data dict, or an error status string."""

    def get(url, params=None, headers=None, cookies=None, timeout=None):
        cid = url.rsplit("/", 1)[1]
        if calls is not None:
            calls.append((cid, params, headers, cookies))
        answer = contents[cid]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, str):
            return Response(200, {"status": answer})
        if isinstance(answer, Response):
            return answer
        return ok(answer)

    share.session.get = get
    return share


ROOT = node_folder("root", [
    node_file("a.pdf", "aaaaaaaa-1111"),
    node_file("a.pdf", "bbbbbbbb-2222"),
    node_folder("Sub", code="SUBCODE"),
    node_folder("Private", canAccess=False),
])
SUB = node_folder("Sub", [node_file("deep: one.txt", "cccccccc-3333", 5, 1_650_000_000)])


def test_links():
    assert gofile.matches("https://gofile.io/d/AbC123") and gofile.matches("gofile.io/d/AbCd1234/")
    assert not gofile.matches("https://gofile.example/d/AbC123")
    assert not gofile.matches("https://drive.example.com/d/" + "x" * 33)
    assert open_url("https://gofile.io/d/AbC123").identity == "gofile:AbC123"


def test_listing_walks_subfolders_and_keeps_duplicates_apart(capsys):
    calls = []
    share = install(gofile.open_share("https://gofile.io/d/Root01"), {"Root01": ROOT, "SUBCODE": SUB}, calls)
    listing = share.list()
    assert listing.share_name == "Root01"  # a folder named "root" takes the id
    assert listing.root_mtime is None
    assert sorted(f.path for f in listing.files) == ["Sub/deep: one.txt", "a (aaaaaaaa).pdf", "a (bbbbbbbb).pdf"]
    deep = next(f for f in listing.files if f.path.startswith("Sub/"))
    assert deep.size == 5 and deep.mtime == 1_650_000_000
    assert [(f.path, f.mtime) for f in listing.folders] == [("Sub", 1_600_000_500)]
    assert "private subfolder: Private" in capsys.readouterr().out
    assert [c[0] for c in calls] == ["Root01", "SUBCODE"]
    _cid, _params, headers, cookies = calls[0]
    assert headers["Authorization"] == f"Bearer {TOKEN}" and cookies == {"accountToken": TOKEN}
    assert headers["X-Website-Token"] == gofile.website_token(TOKEN)


def test_a_named_folder_keeps_its_name_and_a_single_file_gets_its_stem():
    share = install(gofile.open_share("https://gofile.io/d/F1"), {"F1": node_folder("My Stuff", [node_file("x", "1")])})
    assert share.list().share_name == "My Stuff"
    share = install(gofile.open_share("https://gofile.io/d/F2"), {"F2": node_file("Big Book.zip", "2")})
    listing = share.list()
    assert listing.share_name == "Big Book" and [f.path for f in listing.files] == ["Big Book.zip"]


def test_website_token_formula():
    raw = f"{gofile.DEFAULT_UA}::en-US::{TOKEN}::{int(1_700_000_000 / 14400)}::salt"
    assert gofile.website_token(TOKEN, 1_700_000_000) == hashlib.sha256(raw.encode()).hexdigest()


def test_a_password_is_sent_hashed_and_scrubbed():
    calls = []
    share = install(gofile.open_share("https://gofile.io/d/P1", "secret"), {"P1": node_folder("x", [])}, calls)
    share.list()
    assert calls[0][1] == {"password": hashlib.sha256(b"secret").hexdigest()}
    assert "secret" not in " ".join(share.describe())
    assert gofile._scrub("url?password=abc123&x=1") == "url?password=***&x=1"


@pytest.mark.parametrize(
    "status,error,gone",
    [("error-notFound", ShareError, True), ("error-notAuthorized", ShareError, False), ("error-passwordRequired", ShareError, False)],
)
def test_content_errors(status, error, gone):
    share = install(gofile.open_share("https://gofile.io/d/E1"), {"E1": status})
    with pytest.raises(error) as e:
        share.list()
    assert e.value.gone is gone and is_gone_error(e.value) is gone


@pytest.mark.parametrize("answer", [Response(503, text="down"), Response(200, {"status": "error-rateLimit"})])
def test_outages_and_lasting_rate_limits_stop_the_run(answer, monkeypatch):
    monkeypatch.setattr(gofile, "RATE_LIMIT_WAITS", (0, 0))
    share = install(gofile.open_share("https://gofile.io/d/E2"), {"E2": [answer] * 5})
    with pytest.raises(AbortRun):
        share.list()


def test_a_passing_rate_limit_is_waited_out(monkeypatch):
    monkeypatch.setattr(gofile, "RATE_LIMIT_WAITS", (0, 0))
    share = install(gofile.open_share("https://gofile.io/d/R1"), {"R1": ["error-rateLimit", node_folder("x", [node_file("f", "1")])]})
    assert len(share.list().files) == 1


def test_a_rate_limited_subfolder_stops_the_run_rather_than_drop_files(monkeypatch):
    monkeypatch.setattr(gofile, "RATE_LIMIT_WAITS", (0,))
    share = install(gofile.open_share("https://gofile.io/d/S1"), {"S1": ROOT, "SUBCODE": ["error-rateLimit"] * 3})
    with pytest.raises(AbortRun):
        share.list()


def test_a_failing_subfolder_fails_the_share_rather_than_drop_files():
    share = install(gofile.open_share("https://gofile.io/d/S2"), {"S2": ROOT, "SUBCODE": "error-notFound"})
    with pytest.raises(ShareError):
        share.list()


def test_a_refused_token_is_replaced_once(monkeypatch):
    monkeypatch.setattr(gofile, "create_guest_account", lambda session: "N" * 32)
    calls = []
    share = install(gofile.open_share("https://gofile.io/d/W1"), {"W1": ["error-wrongToken", node_folder("x", [])]}, calls)
    share.list()
    assert calls[1][3] == {"accountToken": "N" * 32}
    assert json.loads(gofile.TOKEN_CACHE.read_text())["token"] == "N" * 32


def test_a_stale_salt_is_refreshed_once_per_process(monkeypatch):
    refreshed = []
    salt = {"now": "salt"}
    monkeypatch.setattr(gofile_wt, "current_salt", lambda: salt["now"])

    def refresh(ua):
        refreshed.append(ua)
        salt["now"] = "fresh"
        return "fresh"

    monkeypatch.setattr(gofile_wt, "refresh", refresh)
    share = install(gofile.open_share("https://gofile.io/d/N1"), {"N1": ["error-notPremium", node_folder("x", [])]})
    share.list()
    assert refreshed == [gofile.DEFAULT_UA]
    share = install(gofile.open_share("https://gofile.io/d/N2"), {"N2": ["error-notPremium"]})
    with pytest.raises(ShareError):
        share.list()
    assert len(refreshed) == 1


def test_token_order_is_cache_then_firefox_then_guest(monkeypatch):
    session = requests.Session()
    assert gofile.acquire_token(session) == TOKEN
    gofile._clear_token()
    monkeypatch.setattr(gofile, "firefox_token", lambda: "F" * 32)
    assert gofile.acquire_token(session) == "F" * 32
    gofile._clear_token()
    monkeypatch.setattr(gofile, "firefox_token", lambda: None)
    monkeypatch.setattr(gofile, "create_guest_account", lambda s: "G" * 32)
    assert gofile.acquire_token(session) == "G" * 32


def test_an_old_cached_token_is_not_used(monkeypatch):
    gofile.TOKEN_CACHE.write_text(json.dumps({"token": TOKEN, "created_at": time.time() - 2 * 86400}))
    monkeypatch.setattr(gofile, "create_guest_account", lambda s: "G" * 32)
    assert gofile.acquire_token(requests.Session()) == "G" * 32


def test_the_token_is_never_printed(capsys, monkeypatch):
    gofile._clear_token()
    monkeypatch.setattr(gofile, "create_guest_account", lambda s: "G" * 32)
    share = install(gofile.open_share("https://gofile.io/d/Q1"), {"Q1": node_folder("x", [])})
    share.list()
    assert "GGGG" not in capsys.readouterr().out


def test_firefox_profiles_are_looked_for_on_every_platform(monkeypatch):
    for platform in ("win32", "darwin", "linux"):
        monkeypatch.setattr(gofile.sys, "platform", platform)
        monkeypatch.setenv("APPDATA", "C:/AppData")
        assert gofile.firefox_profile_dirs()


def test_classify_status():
    assert gofile.classify_status("error-notFound") == "not_found"
    assert gofile.classify_status("error-notPremium") == "premium"
    assert gofile.classify_status("error-weird") == "other"


def test_the_salt_is_read_from_what_the_page_hashed():
    probe = gofile_wt.PROBE_TOKEN
    message = f"{gofile.DEFAULT_UA}::en-US::{probe}::123456::newsalt"
    padded = message + "\x80" + "\x00" * 8
    result = {"wt": hashlib.sha256(message.encode()).hexdigest()}
    assert gofile_wt._parse_salt([padded], result, gofile.DEFAULT_UA, "en-US") == "newsalt"
    assert gofile_wt._parse_salt(["unrelated::string::x"], result, gofile.DEFAULT_UA, "en-US") is None
