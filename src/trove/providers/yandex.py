"""Yandex Disk public shares.

``cloud-api.yandex.net/v1/disk/public/resources`` lists a share with no login, a page
at a time, and gives each file a size, a date and SHA-256 and MD5 hashes.

* Some listings are slow, so the read timeout is longer than usual.
* Each download link is requested when it's needed, because the links expire.
* Popular shares can hit a download limit. That fails the file, not the run.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

import requests

from trove.errors import PermanentTransferError
from trove.http import new_session, open_http_transfer, request_with_retry
from trove.model import Listing, RemoteFile, RemoteFolder, Share, Transfer

API = "https://cloud-api.yandex.net/v1/disk/public/resources"
PAGE_SIZE = 200
LIST_TIMEOUT = (15, 120)
_SHARE_PATHS = ("d", "i")


def _with_scheme(url: str) -> str:
    raw = url.strip()
    return raw if "://" in raw else "https://" + raw


def matches(url: str) -> bool:
    parsed = urlparse(_with_scheme(url))
    host = parsed.netloc.lower()
    if host != "yadi.sk" and not host.startswith("disk.yandex."):
        return False
    parts = [p for p in parsed.path.split("/") if p]
    return bool(parts) and parts[0] in _SHARE_PATHS


def open_share(url: str, password: str = "") -> "YandexShare":
    if not matches(url):
        raise ValueError("Expected a yadi.sk/d/ID or disk.yandex.*/d/ID link")
    return YandexShare(_with_scheme(url).split("?", 1)[0].split("#", 1)[0])


def _parse_time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class YandexShare(Share):
    provider_name = "yandex"
    colon = "dash"
    manifest_name = ".yandex-manifest.json"
    verify_kind = "etag"  # the content hash doubles as a change-detection tag
    can_resume = True

    def __init__(self, public_key: str):
        self.public_key = public_key
        self.session = new_session()
        self._single_file = False

    @property
    def identity(self) -> str:
        return f"yandex:{self.public_key.rstrip('/')}"

    def describe(self) -> list[str]:
        return [f"Yandex Disk share: {self.public_key}"]

    def _get(self, path: str | None, offset: int = 0, limit: int = PAGE_SIZE) -> dict:
        params = {"public_key": self.public_key, "limit": limit, "offset": offset}
        if path and path != "/":
            params["path"] = path
        return request_with_retry(self.session, "GET", API, params=params, timeout=LIST_TIMEOUT).json()

    def probe(self) -> None:
        self._get(None, limit=1)

    def list(self, subpath: str = "") -> Listing:
        sub = subpath.strip("/")
        root = self._get(f"/{sub}" if sub else None, limit=PAGE_SIZE)
        share_name = root.get("name") if not sub else self._get(None, limit=1).get("name")
        root_mtime = _parse_time(root.get("modified"))

        if root.get("type") == "file":
            self._single_file = True
            file = self._file(root, root.get("name", "file"))
            # A one-file share gets its own folder, so the orphan check can't scan a whole library.
            stem = (share_name or "file").rsplit(".", 1)[0]
            return Listing([file], [], share_name=stem, root_mtime=root_mtime)

        files: list[RemoteFile] = []
        folders: list[RemoteFolder] = []
        self._walk(f"/{sub}" if sub else "/", root, files, folders)
        return Listing(files, folders, share_name=share_name, root_mtime=root_mtime)

    def _file(self, item: dict, path: str) -> RemoteFile:
        size = int(item["size"]) if item.get("size") is not None else None
        return RemoteFile(path, size, _parse_time(item.get("modified")), item.get("sha256") or item.get("md5"))

    def _walk(self, path: str, first_page: dict | None, files: list[RemoteFile], folders: list[RemoteFolder]) -> None:
        page = first_page or self._get(path)
        offset = 0
        while True:
            items = page.get("_embedded", {}).get("items", [])
            for item in items:
                rel = item["path"].lstrip("/")
                if item.get("type") == "dir":
                    folders.append(RemoteFolder(rel, _parse_time(item.get("modified"))))
                    print(f"  Scanning: {rel}/")
                    self._walk(item["path"], None, files, folders)
                else:
                    files.append(self._file(item, rel))
            offset += len(items)
            if len(items) < PAGE_SIZE:
                return
            page = self._get(path, offset=offset)

    def open_transfer(self, file: RemoteFile, offset: int = 0) -> Transfer:
        params = {"public_key": self.public_key}
        # A one-file share has no path of its own inside the share.
        if not self._single_file:
            params["path"] = "/" + file.path
        try:
            response = request_with_retry(self.session, "GET", f"{API}/download", params=params)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status is not None and status != 429 and status < 500:
                detail = ""
                try:
                    detail = e.response.json().get("description", "")
                except ValueError:
                    pass
                raise PermanentTransferError(f"Yandex refused the download (HTTP {status}): {detail}") from e
            raise
        return open_http_transfer(self.session, response.json()["href"], offset)
