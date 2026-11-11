"""Koofr public links.

A link looks like ``https://app.koofr.net/links/<uuid>``, and is often published through
a shortener. The ``/bundle`` endpoint lists one directory at a time, and gives each file
a size, a date in milliseconds and an MD5 hash.

* A protected link takes its password as a ``?password=`` parameter on every request.
  There is no session to exchange it for. A 401 means it's missing or wrong.
* ``requests`` quotes the URL in its error messages, and those get printed and saved in
  the state file, so every exception leaving this module has the password scrubbed.
* Downloads are refused without ``Referer: <host>/links/<uuid>``. Listings use the bare
  host.
"""

from __future__ import annotations

import re
from typing import Iterator
from urllib.parse import urlparse

import requests

from trove.errors import ShareError
from trove.http import new_session, open_http_transfer, request_with_retry
from trove.model import Listing, RemoteFile, RemoteFolder, Share, Transfer

HOST = "https://app.koofr.net"
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_BARE_ID = re.compile(rf"^{_UUID}$")
_LINK_PATH = re.compile(rf"^/links/({_UUID})(?:/|$)")
_PASSWORD_PARAM = re.compile(r"(password=)[^&\s'\")]+")


def _link_id(url: str) -> str:
    raw = url.strip()
    if _BARE_ID.match(raw):
        return raw.lower()
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    host = parsed.netloc.lower()
    if host != "koofr.net" and not host.endswith(".koofr.net"):
        return ""
    match = _LINK_PATH.match(parsed.path)
    return match.group(1).lower() if match else ""


def matches(url: str) -> bool:
    return bool(_link_id(url))


def open_share(url: str, password: str = "") -> "KoofrShare":
    link_id = _link_id(url)
    if not link_id:
        raise ValueError("Expected a link like https://app.koofr.net/links/UUID")
    return KoofrShare(link_id, password)


def _scrubbed(error: Exception) -> Exception:
    """The same exception, so retry and dead-link handling still work, minus the password."""
    error.args = tuple(_PASSWORD_PARAM.sub(r"\1***", str(arg)) for arg in error.args)
    return error


def _seconds(milliseconds) -> float | None:
    return milliseconds / 1000.0 if milliseconds else None


class KoofrShare(Share):
    provider_name = "koofr"
    colon = "dash"
    verify_kind = "md5"
    can_resume = True

    def __init__(self, link_id: str, password: str = ""):
        self.link_id = link_id
        self.password = password or ""
        self.session = new_session()

    @property
    def identity(self) -> str:
        return f"koofr:{self.link_id}"

    def describe(self) -> list[str]:
        lines = [f"Koofr link: {self.link_id}"]
        if self.password:
            lines.append("Password: (supplied)")
        return lines

    def _params(self, **params) -> dict:
        if self.password:
            params["password"] = self.password
        return params

    def _get(self, endpoint: str, **params) -> dict:
        url = f"{HOST}/api/v2/public/links/{self.link_id}{endpoint}"
        try:
            response = request_with_retry(
                self.session, "GET", url, params=self._params(**params), headers={"Referer": f"{HOST}/"}
            )
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                problem = "wrong password" if self.password else "this link needs a password"
                raise ShareError(f"Koofr: {problem}.") from None
            raise _scrubbed(e) from None
        except requests.exceptions.RequestException as e:
            raise _scrubbed(e) from None
        return response.json()

    def _info(self) -> dict:
        info = self._get("")
        if not info.get("isOnline"):
            raise ShareError("The Koofr link is offline.", gone=True)
        return info

    def probe(self) -> None:
        """A link that wants a password exists, so it counts as usable. The listing
        reports the password problem, which trying a fallback wouldn't solve."""
        try:
            self._info()
        except ShareError as e:
            if e.gone:
                raise

    def list(self, subpath: str = "") -> Listing:
        info = self._info()
        if info.get("message"):
            print(f"  Message: {info['message']}")
        sub = subpath.strip("/")
        files: list[RemoteFile] = []
        folders: list[RemoteFolder] = []
        self._walk(sub, files, folders)
        return Listing(files, folders, share_name=info.get("name"))

    def _walk(self, rel_dir: str, files: list[RemoteFile], folders: list[RemoteFolder]) -> None:
        for entry in self._get("/bundle", path=f"/{rel_dir}").get("files", []):
            rel = f"{rel_dir}/{entry['name']}" if rel_dir else entry["name"]
            mtime = _seconds(entry.get("modified"))
            if entry["type"] == "dir":
                folders.append(RemoteFolder(rel, mtime))
                print(f"  Scanning: {rel}/")
                self._walk(rel, files, folders)
            elif entry["type"] == "file":
                files.append(RemoteFile(rel, entry.get("size"), mtime, entry.get("hash")))

    def open_transfer(self, file: RemoteFile, offset: int = 0) -> Transfer:
        try:
            transfer = open_http_transfer(
                self.session,
                f"{HOST}/content/links/{self.link_id}/files/get",
                offset,
                params=self._params(path=f"/{file.path}"),
                headers={"Referer": f"{HOST}/links/{self.link_id}"},
            )
        except requests.exceptions.RequestException as e:
            raise _scrubbed(e) from None
        return Transfer(_scrubbing(transfer.chunks), transfer.offset)


def _scrubbing(chunks: Iterator[bytes]) -> Iterator[bytes]:
    try:
        yield from chunks
    except requests.exceptions.RequestException as e:
        raise _scrubbed(e) from None
