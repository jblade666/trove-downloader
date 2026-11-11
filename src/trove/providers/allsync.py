"""Allsync and other Nextcloud public shares.

A link looks like ``https://<host>/s/<token>``. It is served over WebDAV at
``/public.php/webdav/``, using HTTP Basic auth with the token as the username and
the share password, or an empty string, as the password.

Things learned the hard way:

* A PROPFIND without a body can return 500 on some hosts, so always send one.
* Busy hosts throttle with 500s, so 429 and 5xx are retried. Other 4xx are not:
  retrying a 401 only delays the error.
* Recurse with Depth 1, since servers may block ``Depth: infinity``.
* The ETag is opaque rather than a content hash, and the server has no checksums.
  It does change when content changes, so ``--verify`` compares it with the value
  recorded at download time.
"""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from trove.http import new_session, open_http_transfer, request_with_retry
from trove.model import Listing, RemoteFile, RemoteFolder, Share, Transfer

WEBDAV_ROOT = "/public.php/webdav"
DAV = "{DAV:}"

# These also use /s/ links, but aren't Nextcloud.
_NOT_NEXTCLOUD = ("terabox",)

_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:resourcetype/><d:getcontentlength/><d:getlastmodified/>"
    "<d:getetag/><d:displayname/>"
    "</d:prop></d:propfind>"
)


def _with_scheme(url: str) -> str:
    raw = url.strip()
    return raw if "://" in raw else "https://" + raw


def _token(url: str) -> str:
    parts = [p for p in urllib.parse.urlparse(_with_scheme(url)).path.split("/") if p]
    if "s" in parts:
        i = parts.index("s")
        if i + 1 < len(parts):
            return parts[i + 1]
    return ""


def matches(url: str) -> bool:
    """Any host with a /s/TOKEN path, except hosts known not to run Nextcloud."""
    host = urllib.parse.urlparse(_with_scheme(url)).netloc.lower()
    if any(other in host for other in _NOT_NEXTCLOUD):
        return False
    return bool(_token(url))


def open_share(url: str, password: str = "") -> "AllsyncShare":
    host = urllib.parse.urlparse(_with_scheme(url)).netloc
    token = _token(url)
    if not host or not token:
        raise ValueError("Expected a link like https://host/s/token")
    return AllsyncShare(host, token, password)


def _parse_modified(prop: ET.Element) -> float | None:
    node = prop.find(f"{DAV}getlastmodified")
    if node is None or not node.text:
        return None
    try:
        return parsedate_to_datetime(node.text).timestamp()
    except (TypeError, ValueError):
        return None


def _href_to_path(href: str) -> str:
    decoded = urllib.parse.unquote(urllib.parse.urlparse(href).path)
    idx = decoded.find(WEBDAV_ROOT)
    if idx != -1:
        decoded = decoded[idx + len(WEBDAV_ROOT) :]
    return decoded.strip("/")


def parse_propfind(xml_bytes: bytes, rel_path: str) -> tuple[dict, list[dict]]:
    """Return ``(self info, children)`` for a Depth-1 PROPFIND response."""
    root = ET.fromstring(xml_bytes)
    self_rel = rel_path.strip("/")
    self_info = {"name": None, "mtime": None}
    entries = []

    for resp in root.findall(f"{DAV}response"):
        href = resp.find(f"{DAV}href")
        if href is None or not href.text:
            continue
        path = _href_to_path(href.text)

        prop = None
        for propstat in resp.findall(f"{DAV}propstat"):
            status = propstat.find(f"{DAV}status")
            if status is not None and status.text and " 200 " in status.text:
                prop = propstat.find(f"{DAV}prop")
                break
        if prop is None:
            continue

        if path == self_rel:
            display = prop.find(f"{DAV}displayname")
            if display is not None and display.text:
                self_info["name"] = display.text
            self_info["mtime"] = _parse_modified(prop)
            continue

        kind = prop.find(f"{DAV}resourcetype")
        is_dir = kind is not None and kind.find(f"{DAV}collection") is not None
        entry = {"path": path, "is_dir": is_dir, "mtime": _parse_modified(prop)}
        if not is_dir:
            size = prop.find(f"{DAV}getcontentlength")
            etag = prop.find(f"{DAV}getetag")
            entry["size"] = int(size.text) if size is not None and size.text else None
            entry["etag"] = etag.text.strip('"') if etag is not None and etag.text else None
        entries.append(entry)

    return self_info, entries


class AllsyncShare(Share):
    provider_name = "allsync"
    colon = "dash"
    manifest_name = ".allsync-manifest.json"
    verify_kind = "etag"
    can_resume = True

    def __init__(self, host: str, token: str, password: str = ""):
        self.host = host
        self.token = token
        self.password = password or ""
        self.session = new_session()

    @property
    def identity(self) -> str:
        return f"allsync:{self.host.lower()}/{self.token}"

    @property
    def _auth(self) -> tuple[str, str]:
        return (self.token, self.password)

    @property
    def _webdav(self) -> str:
        return f"https://{self.host}{WEBDAV_ROOT}"

    def describe(self) -> list[str]:
        lines = [f"Host:  {self.host}", f"Token: {self.token}"]
        if self.password:
            lines.append("Password: (supplied)")
        return lines

    def _propfind(self, rel_path: str = "") -> tuple[dict, list[dict]]:
        sub = urllib.parse.quote(rel_path, safe="/")
        response = request_with_retry(
            self.session,
            "PROPFIND",
            f"{self._webdav}/{sub}",
            auth=self._auth,
            headers={"Depth": "1", "Content-Type": "application/xml"},
            data=_PROPFIND_BODY,
        )
        return parse_propfind(response.content, rel_path)

    def probe(self) -> None:
        self._propfind("")

    def list(self, subpath: str = "") -> Listing:
        sub = subpath.strip("/")
        files: list[RemoteFile] = []
        folders: list[RemoteFolder] = []
        info = self._walk(sub, files, folders)
        # The share's own name comes from its root, even for a subfolder listing.
        name = info["name"] if not sub else self._propfind("")[0]["name"]
        return Listing(files, folders, share_name=name, root_mtime=info["mtime"])

    def _walk(self, rel_path: str, files: list[RemoteFile], folders: list[RemoteFolder]) -> dict:
        info, entries = self._propfind(rel_path)
        for entry in entries:
            if entry["is_dir"]:
                folders.append(RemoteFolder(entry["path"], entry["mtime"]))
                print(f"  Scanning: {entry['path']}/")
                self._walk(entry["path"], files, folders)
            else:
                files.append(RemoteFile(entry["path"], entry["size"], entry["mtime"], entry["etag"]))
        return info

    def open_transfer(self, file: RemoteFile, offset: int = 0) -> Transfer:
        url = f"{self._webdav}/{urllib.parse.quote(file.path, safe='/')}"
        return open_http_transfer(self.session, url, offset, auth=self._auth)
