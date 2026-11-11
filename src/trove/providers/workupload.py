"""workupload.com file links, through a visible browser, with you clicking through
its ad.

A link looks like ``https://workupload.com/file/<id>``. Needs the ``browser`` extra.

* Every page first shows a "Security Check" that its own script passes in the
  browser. trove opens the page in a visible window and waits for that; it never
  solves the check itself.
* The real page gives the file's name, its exact size in bytes and its SHA-256, so
  listing, skipping and ``--verify`` need nothing from you.
* A download is released only after a rewarded ad, which is meant for a person. So
  for each file that needs fetching, trove opens its page and waits while **you**
  click Download and dismiss the ad, then picks up the file. This makes workupload
  sites unsuited to unattended runs.
* Every download is checked against the page's SHA-256 before it's kept.
* Playwright saves to a local temp file that the transfer streams, as for ufile, so
  a failed upload resumes from the temp copy.
* One link is one file, so it gets a folder named after the file without its
  extension, which keeps the orphan check from scanning a whole library.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Iterator
from urllib.parse import urlparse

from trove.browser import UA, apply_stealth, profile_dir, require_display, sync_playwright
from trove.errors import CorruptDownload, PermanentTransferError, ShareError
from trove.model import Listing, RemoteFile, Share, Transfer

PAGE_TIMEOUT = 60_000
CHECK_TIMEOUT = 90_000  # ms for the page to pass its own security check
CLICK_TIMEOUT = 600_000  # ms to wait for you to click Download and dismiss the ad
CHUNK = 1024 * 1024
_FILE = re.compile(r"^/file/([A-Za-z0-9]+)/?$")
CHECK_TITLE = "Are you a human"


def _file_id(url: str) -> str:
    raw = url.strip()
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    host = parsed.netloc.lower()
    if host != "workupload.com" and not host.endswith(".workupload.com"):
        return ""
    match = _FILE.match(parsed.path)
    return match.group(1) if match else ""


def matches(url: str) -> bool:
    return bool(_file_id(url))


def bare_url(text: str) -> str | None:
    """The link a bare file ID stands for (see providers.bare_links)."""
    return f"https://workupload.com/file/{text}" if re.fullmatch(r"[A-Za-z0-9]+", text) else None


def open_share(url: str, password: str = "") -> "WorkuploadShare":
    file_id = _file_id(url)
    if not file_id:
        raise ValueError("Expected a link like https://workupload.com/file/ID")
    if password:
        raise ShareError("workupload links don't take a password here.")
    return WorkuploadShare(file_id)


def parse_page(text: str) -> dict:
    """Name, size and SHA-256 from the file page's text."""
    name = re.search(r"Filename:\s*(.+)", text)
    size = re.search(r"Filesize:\s*(\d+)\s*\(Byte\)", text)
    checksum = re.search(r"Checksum:\s*([0-9a-fA-F]{64})\s*\(SHA256\)", text)
    return {
        "name": name.group(1).strip() if name else None,
        "size": int(size.group(1)) if size else None,
        "sha256": checksum.group(1).lower() if checksum else None,
    }


class WorkuploadShare(Share):
    provider_name = "workupload"
    colon = "dash"
    verify_kind = "sha256"
    can_resume = True  # from the spooled temp file; the browser download itself can't resume

    def __init__(self, file_id: str):
        self.file_id = file_id
        self.url = f"https://workupload.com/file/{file_id}"
        self._pw = self._context = self._page = None
        self._spooled: dict[str, tuple[str, object]] = {}

    @property
    def identity(self) -> str:
        return f"workupload:{self.file_id}"

    def describe(self) -> list[str]:
        return [f"workupload file: {self.file_id}"]

    # -- the browser --------------------------------------------------------------

    def _browser_page(self):
        if self._page is not None:
            return self._page
        require_display("workupload's pages run a security check and its downloads sit behind an ad")
        print("  Opening a browser window for workupload (its page checks the browser first).")
        self._pw = sync_playwright()().start()
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir("workupload"), headless=False, user_agent=UA, locale="en-US",
            accept_downloads=True, viewport={"width": 1280, "height": 900},
        )
        page = self._context.pages[0] if self._context.pages else self._context.new_page()
        apply_stealth(page)
        page.on("dialog", lambda dialog: dialog.dismiss())
        self._page = page
        return page

    def _open_file_page(self) -> str:
        """Load the file page, let it pass its own check, and return its text."""
        page = self._browser_page()
        response = page.goto(self.url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        if response is not None and response.status in (404, 410):
            raise ShareError("The workupload file is gone.", gone=True)
        try:
            page.wait_for_function(f"() => !document.title.includes({CHECK_TITLE!r})", timeout=CHECK_TIMEOUT)
            page.wait_for_load_state("load", timeout=PAGE_TIMEOUT)
        except Exception:
            raise ShareError("workupload's security check didn't finish in the browser.") from None
        return page.inner_text("body")

    # -- listing --------------------------------------------------------------------

    def probe(self) -> None:
        self.list()

    def list(self, subpath: str = "") -> Listing:
        if subpath.strip("/"):
            raise ShareError("A workupload link is one file, so --path doesn't apply.")
        info = parse_page(self._open_file_page())
        if not info["name"]:
            raise ShareError("The workupload page shows no file; it may have been removed.", gone=True)
        file = RemoteFile(info["name"].replace("/", "_"), info["size"], None, info["sha256"], key=self.url)
        stem = os.path.splitext(file.path)[0] or self.file_id
        return Listing([file], [], share_name=stem)

    # -- downloads --------------------------------------------------------------------

    def _download(self, file: RemoteFile) -> str:
        self._open_file_page()
        page = self._page
        print(
            "    In the browser window: click Download, and dismiss the ad when it's done.\n"
            f"    Waiting up to {CLICK_TIMEOUT // 60000} minutes...",
            flush=True,
        )
        try:
            with page.expect_download(timeout=CLICK_TIMEOUT) as info:
                pass
            download = info.value
            path = download.path()  # blocks until the download finishes
        except Exception as e:
            raise PermanentTransferError(f"no download came from the browser ({e.__class__.__name__})") from None
        if not path or not os.path.getsize(path):
            raise PermanentTransferError("the browser download came back empty")
        self._spooled[file.key] = (str(path), download)
        return str(path)

    def open_transfer(self, file: RemoteFile, offset: int = 0) -> Transfer:
        spooled = self._spooled.get(file.key)
        path = spooled[0] if spooled and os.path.exists(spooled[0]) else self._download(file)
        if file.etag and offset == 0:
            with open(path, "rb") as fp:
                digest = _sha256(fp)
            if digest != file.etag.lower():
                self._discard(file.key)
                raise CorruptDownload("the download doesn't match workupload's SHA-256")
        start = min(offset, os.path.getsize(path))
        return Transfer(self._read(file.key, path, start), start)

    def _read(self, key: str, path: str, start: int) -> Iterator[bytes]:
        with open(path, "rb") as fp:
            fp.seek(start)
            for chunk in iter(lambda: fp.read(CHUNK), b""):
                yield chunk
        self._discard(key)  # read through: the engine has it

    def _discard(self, key: str) -> None:
        _path, download = self._spooled.pop(key, (None, None))
        try:
            download.delete()
        except Exception:
            pass

    def close(self) -> None:
        for key in list(self._spooled):
            self._discard(key)
        for thing in (self._context, self._pw):
            if thing is None:
                continue
            try:
                thing.close() if thing is self._context else thing.stop()
            except Exception:
                pass
        self._pw = self._context = self._page = None


def _sha256(fp) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: fp.read(CHUNK), b""):
        digest.update(chunk)
    return digest.hexdigest()
