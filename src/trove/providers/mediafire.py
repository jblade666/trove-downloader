"""MediaFire public folders and files.

``/api/1.5/folder/get_content.php`` lists folders with no login, a chunk at a time,
and gives each file a size, a date and a content hash. The direct download link only
appears on each file's web page, so it is fetched when a file really needs
downloading, never during a listing.

A folder can hold two files with the same name. They are told apart by a suffix from
the file's key, which stays the same from run to run.

With an account (``trove set-password mediafire``), downloads use the API's direct
links instead, which a premium account serves at full speed. Listing stays
anonymous, so the login happens only when a file really needs downloading.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from trove.errors import AbortRun, PermanentTransferError, ShareError
from trove.http import new_session, open_http_transfer, request_with_retry
from trove.model import Listing, RemoteFile, RemoteFolder, Share, Transfer

API = "https://www.mediafire.com/api/1.5"
# The public application ID that MediaFire's own and third-party clients log in with.
APP_ID = "42511"
KEYRING_SERVICE = "trove-mediafire"
EMAIL_ENV = "TROVE_MEDIAFIRE_EMAIL"
PASSWORD_ENV = "TROVE_MEDIAFIRE_PASSWORD"
_TOKEN_EXPIRED = 105  # "The supplied Session Token is expired or invalid"
_SCRAMBLED_RE = re.compile(r'data-scrambled-url="([^"]+)"')
_DIRECT_RE = re.compile(r'href="(https?://download[^"]+)"')


def _with_scheme(url: str) -> str:
    raw = url.strip()
    return raw if "://" in raw else "https://" + raw


def _parse(url: str) -> tuple[str, str] | None:
    """``("folder" or "file", key)``, or None if this isn't a MediaFire share."""
    parsed = urlparse(_with_scheme(url))
    if not parsed.netloc.lower().removeprefix("www.").endswith("mediafire.com"):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "folder":
        return "folder", parts[1]
    if len(parts) >= 2 and parts[0] in ("file", "file_premium"):
        return "file", parts[1]
    return None


def matches(url: str) -> bool:
    return _parse(url) is not None


def open_share(url: str, password: str = "") -> "MediaFireShare":
    parsed = _parse(url)
    if parsed is None:
        raise ValueError("Expected a mediafire.com/folder/KEY or /file/KEY link")
    return MediaFireShare(*parsed)


def _parse_time(item: dict) -> float | None:
    """Prefer ``created_utc``. The plain ``created`` field carries no zone."""
    utc = item.get("created_utc")
    if utc:
        try:
            return datetime.fromisoformat(utc.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    created = item.get("created")
    if created:
        try:
            naive = datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
            return naive.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return None


def _stem(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name.strip(".") else name


def extract_download_url(page: str) -> str | None:
    """Find the direct link on a file's page."""
    match = _SCRAMBLED_RE.search(page)
    if match:
        try:
            return base64.b64decode(match.group(1)).decode()
        except (binascii.Error, ValueError):
            pass
    match = _DIRECT_RE.search(page)
    return html.unescape(match.group(1)) if match else None


def disambiguate(files: list[RemoteFile]) -> None:
    """Add a stable suffix to files sharing a path, ignoring case."""
    groups: dict[str, list[RemoteFile]] = {}
    for f in files:
        groups.setdefault(f.path.lower(), []).append(f)
    for group in groups.values():
        if len(group) < 2:
            continue
        for f in group:
            parent, _, name = f.path.rpartition("/")
            stem, dot, ext = name.rpartition(".")
            suffix = f" ({(f.key or '')[:8]})"
            renamed = f"{stem}{suffix}{dot}{ext}" if stem else f"{name}{suffix}"
            f.path = f"{parent}/{renamed}" if parent else renamed


def find_credentials() -> tuple[str, str] | None:
    """``(email, password)`` from the environment, then the OS keyring; None if either is missing."""
    email, password = os.environ.get(EMAIL_ENV), os.environ.get(PASSWORD_ENV)
    if not (email and password):
        try:
            import keyring

            email = keyring.get_password(KEYRING_SERVICE, "email")
            password = keyring.get_password(KEYRING_SERVICE, "password")
        except Exception:
            return None
    return (email, password) if email and password else None


class MediaFireAccount:
    """A logged-in MediaFire session. Requests are POSTs, so neither the password nor
    the token appears in a URL that an error message could quote."""

    def __init__(self, email: str, password: str, session):
        self._email = email
        self._password = password
        self._session = session
        self._token: str | None = None

    def _post(self, path: str, **data) -> dict:
        response = request_with_retry(self._session, "POST", f"{API}/{path}", data={**data, "response_format": "json"})
        try:
            return response.json().get("response", {})
        except ValueError:
            raise ShareError(f"MediaFire sent an unreadable answer (HTTP {response.status_code})") from None

    def login(self) -> None:
        signature = hashlib.sha1((self._email + self._password + APP_ID).encode()).hexdigest()
        data = self._post("user/get_session_token.php", email=self._email, password=self._password,
                          application_id=APP_ID, signature=signature, token_version="1")
        token = data.get("session_token")
        if data.get("result") != "Success" or not token:
            # Every later download would be refused the same way.
            raise AbortRun(f"MediaFire refused the account login (error {data.get('error')}). "
                           "Check it with 'trove set-password mediafire'.")
        self._token = token

    def _call(self, path: str, **data) -> dict:
        if self._token is None:
            self.login()
        result = self._post(path, session_token=self._token, **data)
        if result.get("result") != "Success" and str(result.get("error")) == str(_TOKEN_EXPIRED):
            self.login()
            result = self._post(path, session_token=self._token, **data)
        return result

    def info(self) -> dict:
        """The account's ``user_info``: ``premium``, ``validated`` and so on."""
        data = self._call("user/get_info.php")
        if data.get("result") != "Success":
            raise ShareError(f"MediaFire said: {data.get('message', 'unknown error')}")
        return data.get("user_info", {})

    def direct_link(self, quick_key: str) -> str:
        data = self._call("file/get_links.php", quick_key=quick_key, link_type="direct_download")
        if data.get("result") != "Success":
            message = data.get("message", "unknown error")
            raise PermanentTransferError(f"MediaFire wouldn't give a download link: {message}")
        links = data.get("links") or []
        url = links[0].get("direct_download") if links else None
        if not url:
            raise PermanentTransferError("MediaFire gave no download link; the file may have been removed")
        return url


class MediaFireShare(Share):
    provider_name = "mediafire"
    colon = "dash"
    manifest_name = ".mediafire-manifest.json"
    verify_kind = "etag"  # the content hash doubles as a change-detection tag
    can_resume = True

    def __init__(self, kind: str, key: str, credentials=None):
        self.kind = kind
        self.key = key
        self.session = new_session()
        self._credentials = credentials
        self._account: MediaFireAccount | None | bool = False  # False: not looked up yet

    def account(self) -> MediaFireAccount | None:
        """The account to download with, if one is set up. Looked up on first use."""
        if self._account is False:
            found = (self._credentials or find_credentials)()
            self._account = MediaFireAccount(*found, self.session) if found else None
        return self._account

    @property
    def identity(self) -> str:
        return f"mediafire:{self.kind}/{self.key}"

    def describe(self) -> list[str]:
        return [f"MediaFire {self.kind}: {self.key}"]

    def _api(self, path: str, **params) -> dict:
        response = request_with_retry(
            self.session, "GET", f"{API}/{path}", params={**params, "response_format": "json"}
        )
        data = response.json().get("response", {})
        if data.get("result") != "Success":
            message = data.get("message", "unknown error")
            gone = bool(re.search(r"not found|invalid|does not exist", message, re.I))
            raise ShareError(f"MediaFire said: {message}", gone=gone)
        return data

    def probe(self) -> None:
        if self.kind == "folder":
            self._api("folder/get_info.php", folder_key=self.key)
        else:
            self._api("file/get_info.php", quick_key=self.key)

    def list(self, subpath: str = "") -> Listing:
        if self.kind == "file":
            return self._list_file()
        info = self._api("folder/get_info.php", folder_key=self.key)["folder_info"]
        files: list[RemoteFile] = []
        folders: list[RemoteFolder] = []
        self._walk(self.key, "", files, folders)
        disambiguate(files)
        sub = subpath.strip("/")
        root_mtime = _parse_time(info)
        if sub:
            prefix = sub + "/"
            root_mtime = next((f.mtime for f in folders if f.path == sub), None)
            files = [f for f in files if f.path.startswith(prefix)]
            folders = [f for f in folders if f.path.startswith(prefix)]
        return Listing(files, folders, share_name=info.get("name"), root_mtime=root_mtime)

    def _list_file(self) -> Listing:
        info = self._api("file/get_info.php", quick_key=self.key)["file_info"]
        name = info["filename"]
        mtime = _parse_time(info)
        file = RemoteFile(name, int(info.get("size") or 0), mtime, info.get("hash"), self.key)
        # A one-file share gets its own folder, so the orphan check can't scan a whole library.
        return Listing([file], [], share_name=_stem(name), root_mtime=mtime)

    def _walk(self, key: str, prefix: str, files: list[RemoteFile], folders: list[RemoteFolder]) -> None:
        for item in self._content(key, "files"):
            path = f"{prefix}{item['filename']}"
            size = int(item["size"]) if item.get("size") else None
            files.append(RemoteFile(path, size, _parse_time(item), item.get("hash"), item.get("quickkey")))
        for item in self._content(key, "folders"):
            path = f"{prefix}{item['name']}"
            folders.append(RemoteFolder(path, _parse_time(item)))
            print(f"  Scanning: {path}/")
            self._walk(item["folderkey"], path + "/", files, folders)

    def _content(self, key: str, kind: str) -> list[dict]:
        items: list[dict] = []
        chunk = 1
        while True:
            data = self._api(
                "folder/get_content.php", folder_key=key, content_type=kind, chunk=chunk, chunk_size=1000
            )
            content = data.get("folder_content", {})
            items.extend(content.get(kind) or [])
            if content.get("more_chunks") != "yes":
                return items
            chunk += 1

    def open_transfer(self, file: RemoteFile, offset: int = 0) -> Transfer:
        account = self.account()
        if account is not None:
            return open_http_transfer(self.session, account.direct_link(file.key), offset)
        page = request_with_retry(self.session, "GET", f"https://www.mediafire.com/file/{file.key}/file")
        url = extract_download_url(page.text)
        if not url:
            raise PermanentTransferError("No download link on the file's page; it may have been removed")
        return open_http_transfer(self.session, url, offset)
