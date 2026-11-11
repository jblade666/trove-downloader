"""gofile.io folders and files.

A link looks like ``https://gofile.io/d/<id>``; ids have been 6 and then 8 letters and
digits, and any length works here. Listings come from ``GET api.gofile.io/contents/<id>``
and need an account token, which any guest account has:

* The token comes from the cache (one day old at most), else the gofile login in a
  Firefox profile, else a new guest account. It's cached in trove's cache folder,
  which is the one place it's kept, so a refresh by one share serves the next. It's
  a credential, so it's never printed.
* Every API call is signed with ``X-Website-Token = sha256("{UA}::en-US::{token}::
  {time slot}::{salt}")``, the slot being 4 hours. gofile rotates the salt every few
  months and reports a stale one as ``error-notPremium`` (or ``error-wrongToken``).
  ``gofile_wt`` re-extracts it from the live site once per process, which needs the
  ``browser`` extra (Playwright).
* Errors are classified once, from gofile's status string and the HTTP status.
  A dead link is gone. A rate limit, an outage, a network failure, or a token still
  refused after a refresh stop the run, since every other share would fail too.
* A folder's children may come without their own children; those are fetched one
  call each, spaced out, and a rate-limited fetch waits and retries. One that still
  fails stops the share or the run. Dropping it would lose its files and make them
  look like orphans.
* A link to one file gets a folder named after the file without its extension.
  Folders named ``root`` use the content id, as before.
* gofile allows two files with one name in a folder. Each gets `` (<first 8 of its
  id>)`` before the extension, which stays the same between runs.
* Dates are ``createTime``, for files and folders. The top folder gets none.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from platformdirs import user_cache_dir

from trove.errors import AbortRun, ShareError
from trove.http import API_TIMEOUT, open_http_transfer
from trove.model import Listing, RemoteFile, RemoteFolder, Share, Transfer
from trove.providers import gofile_wt

API = "https://api.gofile.io"
ORIGIN = "https://gofile.io"
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) Gecko/20100101 Firefox/151.0"
TOKEN_MAX_AGE = 24 * 3600
TOKEN_CACHE = Path(user_cache_dir("trove-downloader", appauthor=False)) / "gofile-token.json"
SUBFOLDER_DELAY = 1.0  # seconds before each subfolder call, to stay under the rate limit
RATE_LIMIT_WAITS = (15, 30, 60, 90)  # the limit is a refilling bucket, so waiting clears it
_LINK = re.compile(r"^/d/([A-Za-z0-9]+)/?$")
_BARE_ID = re.compile(r"^[A-Za-z0-9]+$")
_PASSWORD_PARAM = re.compile(r"(password=)[^&\s'\")]+")
SYSTEMIC = ("rate_limited", "unavailable", "network")

# Per process, not per share: the salt is gofile's, and re-extracting it launches a
# browser, so a batch must not do it once per share.
_salt_checked = False


def _content_id(url: str) -> str:
    raw = url.strip()
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    if parsed.netloc.lower() not in ("gofile.io", "www.gofile.io"):
        return ""
    match = _LINK.match(parsed.path)
    return match.group(1) if match else ""


def matches(url: str) -> bool:
    return bool(_content_id(url))


def bare_url(text: str) -> str | None:
    """The link a bare content ID stands for (see providers.bare_links)."""
    return f"https://gofile.io/d/{text}" if _BARE_ID.match(text) else None


def open_share(url: str, password: str = "") -> "GofileShare":
    content_id = _content_id(url)
    if not content_id:
        raise ValueError("Expected a link like https://gofile.io/d/ID")
    return GofileShare(content_id, password)


# -- account token ----------------------------------------------------------------


def _load_token() -> str | None:
    try:
        data = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
        if time.time() - data["created_at"] < TOKEN_MAX_AGE:
            return data["token"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _save_token(token: str) -> None:
    try:
        TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE.write_text(json.dumps({"token": token, "created_at": time.time()}), encoding="utf-8")
    except OSError:
        pass


def _clear_token() -> None:
    try:
        TOKEN_CACHE.unlink()
    except OSError:
        pass


def firefox_profile_dirs() -> list[Path]:
    home = Path.home()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return [Path(appdata) / "Mozilla" / "Firefox" / "Profiles"] if appdata else []
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "Firefox" / "Profiles"]
    return [
        home / ".mozilla" / "firefox",
        home / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
        home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
    ]


def firefox_token() -> str | None:
    """The token gofile's site keeps in Firefox's localStorage.

    The value may be LZ4-compressed, but the token survives as a literal run, so a
    search of the raw bytes finds it. ``appdataAccount`` holds the active session and
    is preferred over ``accountsObject``, which can hold stale tokens. The database
    is copied first, so this works while Firefox is running.
    """
    for profiles in firefox_profile_dirs():
        if not profiles.is_dir():
            continue
        for db in sorted(profiles.glob("*/storage/default/https+++gofile.io/ls/data.sqlite")):
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as fp:
                    tmp = fp.name
                shutil.copy2(db, tmp)
                conn = sqlite3.connect(tmp)
                try:
                    rows = conn.execute(
                        "SELECT value FROM data WHERE key IN ('appdataAccount','accountsObject')"
                        " ORDER BY CASE key WHEN 'appdataAccount' THEN 0 ELSE 1 END"
                    ).fetchall()
                finally:
                    conn.close()
                for (raw,) in rows:
                    raw = raw if isinstance(raw, bytes) else str(raw).encode()
                    # The key itself may be partly back-referenced by the compression.
                    for key in (rb'"token"', rb'token"', rb'oken"', rb'ken"'):
                        match = re.search(key + rb'\s*:\s*"([A-Za-z0-9]{20,60})"', raw)
                        if match:
                            return match.group(1).decode()
            except (OSError, sqlite3.Error):
                continue
            finally:
                if tmp:
                    Path(tmp).unlink(missing_ok=True)
    return None


def create_guest_account(session: requests.Session, attempts: int = 5) -> str:
    wait = 30
    for attempt in range(attempts):
        try:
            response = session.post(f"{API}/accounts", timeout=API_TIMEOUT)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt == attempts - 1:
                raise AbortRun(f"Can't reach gofile ({e.__class__.__name__}).") from None
            response = None
        if response is not None and response.status_code != 429:
            if response.status_code in (502, 503, 504):
                raise AbortRun("gofile's API is unavailable; try again later.")
            try:
                data = response.json()
            except ValueError:
                data = {}
            if data.get("status") == "ok":
                return data["data"]["token"]
            raise ShareError(f"gofile wouldn't create a guest account ({data.get('status') or response.status_code}).")
        if attempt < attempts - 1:
            print(f"  gofile: busy creating a guest account; retry in {wait}s...", flush=True)
            time.sleep(wait)
            wait = min(wait * 2, 300)
    raise AbortRun("gofile rate-limited guest account creation, even after retries.")


def acquire_token(session: requests.Session) -> str:
    token = _load_token()
    if token:
        return token
    token = firefox_token()
    if token:
        print("  Using the gofile login from Firefox.")
    else:
        print("  Creating a gofile guest account...")
        token = create_guest_account(session)
    _save_token(token)
    return token


def refresh_token(session: requests.Session, refused: str) -> str:
    _clear_token()
    token = firefox_token()
    if not token or token == refused:
        print("  Creating a new gofile guest account...")
        token = create_guest_account(session)
    _save_token(token)
    return token


def website_token(account_token: str, now: float | None = None) -> str:
    slot = int((now if now is not None else time.time()) / 14400)
    raw = f"{DEFAULT_UA}::en-US::{account_token}::{slot}::{gofile_wt.current_salt()}"
    return hashlib.sha256(raw.encode()).hexdigest()


# -- errors -----------------------------------------------------------------------


class ApiError(Exception):
    """kind: network, rate_limited, unavailable, not_found, not_authorized, wrong_token,
    premium or other. Callers branch on the kind, never the message."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def classify_status(status: str) -> str:
    s = status.lower().replace("-", "")
    for marker, kind in (
        ("notfound", "not_found"),
        ("wrongtoken", "wrong_token"),
        ("notauthorized", "not_authorized"),
        ("password", "not_authorized"),
        ("ratelimit", "rate_limited"),
        ("premium", "premium"),
    ):
        if marker in s:
            return kind
    return "other"


def _scrub(text: str) -> str:
    return _PASSWORD_PARAM.sub(r"\g<1>***", text)


# -- the share --------------------------------------------------------------------


class GofileShare(Share):
    provider_name = "gofile"
    colon = "dash"
    can_resume = True

    def __init__(self, content_id: str, password: str = ""):
        self.content_id = content_id
        self.password = password or ""
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_UA, "Origin": ORIGIN, "Referer": f"{ORIGIN}/"})
        self._token: str | None = None

    @property
    def identity(self) -> str:
        return f"gofile:{self.content_id}"

    def describe(self) -> list[str]:
        lines = [f"gofile content: {self.content_id}"]
        if self.password:
            lines.append("Password: (supplied)")
        return lines

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = acquire_token(self.session)
        return self._token

    # -- API ------------------------------------------------------------------

    def _get_once(self, content_id: str) -> dict:
        headers = {
            "X-BL": "en-US",
            "Authorization": f"Bearer {self.token}",
            "X-Website-Token": website_token(self.token),
        }
        params = {"password": hashlib.sha256(self.password.encode()).hexdigest()} if self.password else {}
        try:
            response = self.session.get(
                f"{API}/contents/{content_id}", params=params, headers=headers,
                cookies={"accountToken": self.token}, timeout=API_TIMEOUT,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            raise ApiError("network", f"can't reach gofile ({e.__class__.__name__})") from None
        except requests.exceptions.RequestException as e:
            raise ApiError("other", _scrub(str(e))) from None
        try:
            body = response.json()
        except ValueError:
            body = None
        status = (body or {}).get("status") if isinstance(body, dict) else None
        if status == "ok":
            return body["data"]
        if status:
            raise ApiError(classify_status(status), status)
        if response.status_code == 429:
            raise ApiError("rate_limited", "HTTP 429")
        if response.status_code in (502, 503, 504) or "currently unavailable" in (response.text or "").lower():
            raise ApiError("unavailable", "gofile's API is unavailable")
        raise ApiError("other", f"HTTP {response.status_code}")

    def _get(self, content_id: str, what: str = "content") -> dict:
        """One content call, waiting out a passing rate limit."""
        for wait in (*RATE_LIMIT_WAITS, None):
            try:
                return self._get_once(content_id)
            except ApiError as e:
                if e.kind != "rate_limited" or wait is None:
                    raise
                print(f"    gofile: rate-limited on {what}; retry in {wait}s...", flush=True)
                time.sleep(wait)
        raise AssertionError("unreachable")

    def _root(self) -> dict:
        global _salt_checked
        try:
            return self._get(self.content_id)
        except ApiError as e:
            error = e
        if error.kind == "wrong_token":
            print("  gofile refused the token; getting a fresh one...")
            self._token = refresh_token(self.session, self.token)
            try:
                return self._get(self.content_id)
            except ApiError as e:
                error = e
        if error.kind in ("premium", "wrong_token") and not _salt_checked:
            # What a stale website-token salt looks like from here.
            _salt_checked = True
            print(f"  gofile said {error}; checking the website-token salt...")
            previous = gofile_wt.current_salt()
            fresh = gofile_wt.refresh(DEFAULT_UA)
            if fresh and fresh != previous:
                try:
                    return self._get(self.content_id)
                except ApiError as e:
                    error = e
        raise self._share_error(error)

    def _share_error(self, error: ApiError) -> Exception:
        if error.kind == "not_found":
            return ShareError("The gofile content doesn't exist; it may have been deleted or expired.", gone=True)
        if error.kind == "not_authorized":
            problem = "wrong password" if self.password else "it's private or needs a password"
            return ShareError(f"gofile won't show this content: {problem}.")
        if error.kind == "wrong_token":
            return AbortRun("gofile refused the token even after refreshing the token and the website-token salt; "
                            "its signing scheme has probably changed.")
        if error.kind in SYSTEMIC:
            return AbortRun(f"gofile can't be used right now ({error}).")
        if error.kind == "premium":
            return ShareError("gofile says this needs a premium account (the website-token salt was checked).")
        return ShareError(f"gofile: {error}")

    def probe(self) -> None:
        self._root()

    # -- listing --------------------------------------------------------------

    def list(self, subpath: str = "") -> Listing:
        data = self._root()
        files: list[RemoteFile] = []
        folders: list[RemoteFolder] = []
        if data.get("type") == "file":
            name = data.get("name") or self.content_id
            share_name = os.path.splitext(name)[0] or self.content_id
            self._walk(data, "", files, folders)
        else:
            share_name = data.get("name") or self.content_id
            if share_name == "root":
                share_name = self.content_id
            for child in _children(data):
                self._walk(child, "", files, folders)
        _disambiguate(files)
        sub = subpath.strip("/")
        if sub:
            prefix = sub + "/"
            files = [f for f in files if f.path.startswith(prefix)]
            folders = [f for f in folders if f.path == sub or f.path.startswith(prefix)]
        return Listing(files, folders, share_name=share_name)

    def _walk(self, node: dict, prefix: str, files: list[RemoteFile], folders: list[RemoteFolder]) -> None:
        name = node.get("name") or ""
        if node.get("type") == "file":
            files.append(RemoteFile(f"{prefix}{name}", node.get("size"), node.get("createTime"), key=_file_key(node)))
            return
        if node.get("canAccess") is False:
            print(f"  Skipping a private subfolder: {name or '(unnamed)'}")
            return
        path = f"{prefix}{name}" if name else prefix.rstrip("/")
        if name:
            folders.append(RemoteFolder(path, node.get("createTime")))
        children = _children(node)
        if not children and node.get("code"):
            print(f"  Scanning: {path}/")
            time.sleep(SUBFOLDER_DELAY)
            try:
                children = _children(self._get(node["code"], "a subfolder"))
            except ApiError as e:
                if e.kind == "rate_limited":
                    raise AbortRun("gofile kept rate-limiting subfolder fetches (the address is likely "
                                   "temporarily banned); stopping rather than sync part of the tree.") from None
                raise self._share_error(e) from None
        for child in children:
            self._walk(child, f"{path}/" if path else "", files, folders)

    # -- downloads ------------------------------------------------------------

    def open_transfer(self, file: RemoteFile, offset: int = 0) -> Transfer:
        link, _, _id = file.key.partition("\n")
        return open_http_transfer(self.session, link, offset, cookies={"accountToken": self.token})


def _children(node: dict) -> list[dict]:
    children = node.get("children") or {}
    return list(children.values()) if isinstance(children, dict) else list(children)


def _file_key(node: dict) -> str:
    """The download link, then the id, which names a duplicate."""
    return f"{node.get('link') or ''}\n{node.get('id') or ''}"


def _short_id(file: RemoteFile) -> str:
    link, _, file_id = file.key.partition("\n")
    if not file_id:
        match = re.search(r"/([a-f0-9-]{36})/", link)
        file_id = match.group(1) if match else ""
    return file_id[:8] or "dup"


def _disambiguate(files: list[RemoteFile]) -> None:
    """Two files at one path both get `` (<id>)``, stable whatever the API's order."""
    groups: dict[str, list[RemoteFile]] = {}
    for f in files:
        groups.setdefault(f.path, []).append(f)
    for group in groups.values():
        if len(group) < 2:
            continue
        for f in group:
            folder, slash, name = f.path.rpartition("/")
            dot = name.rfind(".")
            name = f"{name[:dot]} ({_short_id(f)}){name[dot:]}" if dot > 0 else f"{name} ({_short_id(f)})"
            f.path = f"{folder}{slash}{name}"
