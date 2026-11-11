"""ufile.io folders and files, through a real browser.

Links: ``ufile.io/f/<slug>`` for a folder, ``ufile.io/<slug>`` for one file (file slugs
are 8 letters and digits). This needs the ``browser`` extra (Playwright).

* Listings are scraped from the page in a headless browser. ufile's markup isn't an
  API, so each extractor tries several selectors.
* The sizes shown are rounded ("9.0 MB"), so they're never compared with a file on
  disk: the listing gives no size, and a file that's there and not empty counts as
  present. The old tool compared them and so fetched every file again.
* Downloads go through ufile's free-download flow (button, ad popup, offer modal,
  reCAPTCHA) in a **visible** browser, because the reCAPTCHA can need solving by
  hand and the CDN link only works in the browser that solved it. So downloading
  can't run on a headless server; listing and dry runs can.
* The browser keeps a profile in trove's data folder, so reCAPTCHA trust builds up
  between runs. It opens on the first real download, never in a dry run.
* Playwright downloads to a local temp file, which the transfer streams to the
  destination; the engine writes it to ``.part`` and renames it. The temp file is
  kept until it has been read through, so a failed upload over SFTP resumes without
  downloading again.
* A file's date is only on its own page, so it's known once the file is downloaded.
  Files already there keep theirs.
* A folder named after its slug, or a number, uses the slug. A one-file link gets a
  folder named after the file without its extension.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Iterator
from urllib.parse import urlparse

from trove.browser import UA, apply_stealth, has_display, profile_dir, sync_playwright
from trove.errors import PermanentTransferError, ShareError
from trove.model import Listing, RemoteFile, Share, Transfer

PAGE_TIMEOUT = 60_000
CAPTCHA_TIMEOUT = 180_000  # ms to wait for the download to start, including a hand-solved reCAPTCHA
CHUNK = 1024 * 1024
_FILE_SLUG = re.compile(r"^/([A-Za-z0-9]{8})/?$")
# Site pages whose names are also 8 letters, so they'd pass for file slugs.
_SITE_PAGES = {"business", "features", "register", "password", "settings", "download", "uploader", "contacts"}
_FOLDER = re.compile(r"^/f/([A-Za-z0-9_-]+)/?$")


def _parse(url: str) -> tuple[str, str] | None:
    raw = url.strip()
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    if parsed.netloc.lower() not in ("ufile.io", "www.ufile.io"):
        return None
    folder = _FOLDER.match(parsed.path)
    if folder:
        return "folder", folder.group(1)
    file = _FILE_SLUG.match(parsed.path)
    return ("file", file.group(1)) if file and file.group(1).lower() not in _SITE_PAGES else None


def matches(url: str) -> bool:
    return _parse(url) is not None


def bare_url(text: str) -> str | None:
    """The link a bare slug stands for: ``f/SLUG`` is a folder, an 8-letter slug a
    file (see providers.bare_links)."""
    link = f"https://ufile.io/{text}"
    return link if _parse(link) else None


def open_share(url: str, password: str = "") -> "UfileShare":
    parts = _parse(url)
    if not parts:
        raise ValueError("Expected a link like https://ufile.io/f/SLUG or https://ufile.io/FILESLUG")
    if password:
        raise ShareError("ufile links don't take a password here.")
    return UfileShare(*parts)


def parse_created(text: str) -> float | None:
    """The page's "File created: 4/13/2026, 1:13:26 PM", in local time."""
    match = re.search(r"File created:\s*(\d{1,2}/\d{1,2}/\d{4},?\s*\d{1,2}:\d{2}:\d{2}\s*[APap][Mm])", text or "")
    if match:
        for fmt in ("%m/%d/%Y, %I:%M:%S %p", "%m/%d/%Y %I:%M:%S %p"):
            try:
                return datetime.strptime(match.group(1), fmt).timestamp()
            except ValueError:
                continue
    match = re.search(r"File created:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", text or "")
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M").timestamp() if match else None


class UfileShare(Share):
    provider_name = "ufile"
    colon = "dash"
    can_resume = True  # from the spooled temp file; the browser download itself can't resume

    def __init__(self, kind: str, slug: str):
        self.kind, self.slug = kind, slug
        self._pw = None
        self._context = None
        self._page = None
        self._spooled: dict[str, tuple[str, object]] = {}  # file key -> (temp path, playwright download)

    @property
    def identity(self) -> str:
        return f"ufile:{self.kind}:{self.slug}"

    def describe(self) -> list[str]:
        return [f"ufile {self.kind}: {self.slug}"]

    @property
    def url(self) -> str:
        return f"https://ufile.io/f/{self.slug}" if self.kind == "folder" else f"https://ufile.io/{self.slug}"

    # -- listing (headless) -----------------------------------------------------

    def probe(self) -> None:
        self.list()

    def list(self, subpath: str = "") -> Listing:
        if subpath.strip("/"):
            raise ShareError("ufile folders have no subfolders, so --path doesn't apply.")
        with sync_playwright()() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_context(user_agent=UA, locale="en-US").new_page()
                page.on("dialog", lambda dialog: dialog.dismiss())
                print(f"  Loading {self.url}")
                response = page.goto(self.url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
                if response is not None and response.status in (404, 410):
                    raise ShareError("The ufile link is gone.", gone=True)
                if self.kind == "folder":
                    return self._list_folder(page)
                return self._list_file(page)
            finally:
                browser.close()

    def _list_folder(self, page) -> Listing:
        for selector in (".file-wrapper", ".folder", ".file", "[class*='file']", "table"):
            try:
                page.wait_for_selector(selector, timeout=10_000)
                break
            except Exception:
                continue
        name = self.slug
        for selector in ("h1", "h2", ".folder-name", "[class*='title']", ".heading"):
            try:
                el = page.query_selector(selector)
                text = el.inner_text().strip() if el else ""
            except Exception:
                text = ""
            if text and len(text) < 200 and not text.isdigit():
                name = text
                break
        files, seen = [], set()
        for item in page.evaluate(_LIST_JS) or []:
            file_name = (item.get("name") or "").split("\n")[0].strip()
            link = item.get("link") or ""
            if not file_name or not link or file_name in seen:
                continue
            seen.add(file_name)
            files.append(RemoteFile(file_name, None, None, key=link))
        if name == self.slug or name.isdigit():
            name = self.slug
        return Listing(files, [], share_name=name)

    def _list_file(self, page) -> Listing:
        name = _file_page_name(page) or self.slug
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        file = RemoteFile(name, None, parse_created(body), key=self.url)
        stem = os.path.splitext(name)[0] or self.slug
        return Listing([file], [], share_name=stem)

    # -- downloads (visible browser) ----------------------------------------------

    def _browser_page(self):
        if self._page is not None:
            return self._page
        if not has_display():
            raise ShareError(
                "ufile downloads need a visible browser (its reCAPTCHA may need solving by hand), "
                "so they can't run on a headless server. Listing and --dry-run still work."
            )
        print("  Opening a browser window: ufile's reCAPTCHA may need solving by hand for each file.")
        self._pw = sync_playwright()().start()
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir("ufile"), headless=False, user_agent=UA, locale="en-US",
            accept_downloads=True, viewport={"width": 1280, "height": 900},
        )
        page = self._context.new_page()
        apply_stealth(page)
        page.on("dialog", lambda dialog: dialog.dismiss())

        def close_popups(new_page):
            if new_page != page:
                try:
                    new_page.close()
                except Exception:
                    pass

        self._context.on("page", close_popups)
        self._page = page
        return page

    def _download(self, file: RemoteFile) -> str:
        """Walk the free-download flow and return the local temp file."""
        page = self._browser_page()
        try:
            page.goto(file.key, wait_until="networkidle", timeout=PAGE_TIMEOUT)
            body = page.inner_text("body")
            if "Premium Access Only" in body or "hosting period for this file has now expired" in body:
                raise PermanentTransferError("ufile only offers this file to premium accounts now: its free hosting period ended")
            created = parse_created(body)
            if created:
                file.mtime = created  # the engine dates the file from this once it's written
            button = page.wait_for_selector("a.slow-download-button.free-download", timeout=10_000)
            page.wait_for_timeout(1000)
            box = button.bounding_box()
            if box:
                page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                page.wait_for_timeout(500)
            button.click()  # the first click often only opens an ad
            page.wait_for_timeout(3000)
            offer = page.query_selector(".download-offer")
            if not (offer and page.evaluate("el => getComputedStyle(el).visibility === 'visible'", offer)):
                again = page.query_selector("a.slow-download-button.free-download")
                if again:
                    again.click(force=True)
                    page.wait_for_timeout(2000)
            slow = page.query_selector(".download-offer a.download-button")
            if not slow:
                raise PermanentTransferError("ufile's page has no free-download option for this file")
            print("    Solve the reCAPTCHA in the browser window if it asks...", flush=True)
            with page.expect_download(timeout=CAPTCHA_TIMEOUT) as info:
                slow.click(force=True)
            download = info.value
            path = download.path()  # blocks until the download finishes
        except PermanentTransferError:
            raise
        except Exception as e:
            raise PermanentTransferError(f"the browser download failed: {e.__class__.__name__}: {e}") from None
        if not path or not os.path.getsize(path):
            raise PermanentTransferError("the browser download came back empty")
        self._spooled[file.key] = (str(path), download)
        return str(path)

    def open_transfer(self, file: RemoteFile, offset: int = 0) -> Transfer:
        spooled = self._spooled.get(file.key)
        path = spooled[0] if spooled and os.path.exists(spooled[0]) else self._download(file)
        file.size = os.path.getsize(path)  # now known exactly; the engine checks the copy against it
        start = min(offset, file.size)
        return Transfer(self._read(file.key, path, start), start)

    def _read(self, key: str, path: str, start: int) -> Iterator[bytes]:
        with open(path, "rb") as fp:
            fp.seek(start)
            for chunk in iter(lambda: fp.read(CHUNK), b""):
                yield chunk
        # Read through: the engine has it, so the temp copy can go.
        _path, download = self._spooled.pop(key, (None, None))
        try:
            download.delete()
        except Exception:
            pass

    def close(self) -> None:
        for _path, download in self._spooled.values():
            try:
                download.delete()
            except Exception:
                pass
        self._spooled.clear()
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._context = self._page = self._pw = None


def _file_page_name(page) -> str | None:
    try:
        el = page.query_selector("a.slow-download-button.free-download, a[data-filename]")
        name = (el.get_attribute("data-filename") or "").strip() if el else ""
        if name:
            return name
    except Exception:
        pass
    for selector in (".file-name", ".filename", "h1.name", "h1.filename", ".name", "h1", "h2"):
        try:
            el = page.query_selector(selector)
            text = (el.inner_text() or "").split("\n")[0].strip() if el else ""
        except Exception:
            continue
        if text and "." in text and len(text) < 300 and not text.lower().startswith("ufile"):
            return text
    try:
        title = (page.title() or "").strip()
    except Exception:
        title = ""
    for sep in (" - ", " | ", " — "):
        title = title.split(sep)[0].strip()
    return title if title and "." in title and len(title) < 300 else None


# Reads the file list from a folder page: ufile's .file-wrapper entries, then table
# rows, then any link that looks like a file page.
_LIST_JS = r"""() => {
    const results = [];
    for (const wrapper of document.querySelectorAll('.file-wrapper, .file')) {
        const linkEl = wrapper.querySelector('a[href]');
        if (!linkEl) continue;
        const nameEl = wrapper.querySelector('.name');
        let name = nameEl
            ? (nameEl.childNodes[0]?.textContent?.trim() || nameEl.textContent.split('\n')[0].trim())
            : linkEl.textContent.split('\n')[0].trim();
        if (name && linkEl.href) results.push({name, link: linkEl.href, allText: wrapper.innerText || ''});
    }
    if (results.length === 0) {
        for (const row of document.querySelectorAll('tr, [class*="file-row"]')) {
            const link = row.querySelector('a[href]');
            if (!link) continue;
            const name = link.textContent.split('\n')[0].trim();
            if (name && link.href && !link.href.includes('/f/')) {
                results.push({name, link: link.href, allText: row.innerText || ''});
            }
        }
    }
    if (results.length === 0) {
        for (const link of document.querySelectorAll('a[href]')) {
            const href = link.href;
            const name = link.textContent.split('\n')[0].trim();
            if (name && name.length > 2 && /ufile\.io\/[a-z0-9]+$/i.test(href)
                && !/\/(f|terms|privacy|faq|pricing|business)(\/|$)/.test(new URL(href).pathname)) {
                results.push({name, link: href, allText: link.closest('div, tr, li')?.innerText || ''});
            }
        }
    }
    return results;
}"""
