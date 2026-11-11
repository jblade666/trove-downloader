"""Links published inside a document.

Some sharers keep one stable link that leads to a small document, usually a PDF on
MEGA or a one-link web page, and put the current share link inside it, changing it
whenever they like. A
site with ``link_in_document`` follows its ``url`` to that document, reads it, and
uses the first link in it on one of the listed hosts. That link then goes through
the normal path, shorteners included. If any step fails, the site's ``fallback``
links are tried as usual, so the last known link still works.

The document is fetched with the provider its link names, so MEGA's decryption and
MAC check apply. PDFs are read without a PDF library: ``/URI`` actions are taken from
the raw bytes and from every Flate stream, in literal or hex form.
"""

from __future__ import annotations

import re
import zlib
from html import unescape
from urllib.parse import urljoin, urlparse

from trove.errors import ShareError

MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_PAGE_BYTES = 2 * 1024 * 1024
_HREF = re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)

# A /URI action's value is a literal string, /URI (https://...), where a backslash
# escapes the next byte, or a hex string, /URI <68747470...>.
_URI = re.compile(rb"/URI\s*(?:\(((?:\\.|[^\\)])*)\)|<([0-9A-Fa-f\s]+)>)", re.S)
_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}


def _unescape(raw: bytes) -> bytes:
    out, i = bytearray(), 0
    while i < len(raw):
        c = raw[i : i + 1]
        if c != b"\\":
            out += c
            i += 1
            continue
        nxt = raw[i + 1 : i + 2]
        if nxt.isdigit():  # \ddd octal
            digits = raw[i + 1 : i + 4]
            octal = digits[: len(digits) - len(digits.lstrip(b"01234567"))] or b"0"
            out.append(int(octal, 8) & 0xFF)
            i += 1 + len(octal)
        elif nxt in (b"\n", b"\r"):  # line continuation
            i += 2
        else:
            out += _ESCAPES.get(nxt, nxt)
            i += 2
    return bytes(out)


def _segments(pdf: bytes):
    """The raw file, then every Flate stream inflated, since newer producers pack
    link annotations into compressed object streams."""
    yield pdf
    for match in re.finditer(rb"stream\r?\n", pdf):
        end = pdf.find(b"endstream", match.end())
        if end == -1:
            continue
        try:
            yield zlib.decompressobj().decompress(pdf[match.end() : end])
        except zlib.error:
            pass


def pdf_links(pdf: bytes) -> list[str]:
    """Every link target in a PDF, in file order, without repeats."""
    seen, out = set(), []
    for segment in _segments(pdf):
        for literal, hexed in _URI.findall(segment):
            raw = _unescape(literal) if literal else bytes.fromhex(re.sub(rb"\s", b"", hexed).decode("ascii"))
            url = raw.decode("utf-8", "replace").strip()
            if url and url not in seen:
                seen.add(url)
                out.append(url)
    return out


def _on_host(url: str, hosts: list[str]) -> bool:
    host = urlparse(url).netloc.lower().split(":")[0]
    return any(host == h or host.endswith("." + h) for h in (h.lower().strip(".") for h in hosts))


def pick_link(links: list[str], hosts: list[str]) -> str:
    hits = [u for u in links if _on_host(u, hosts)]
    if not hits:
        raise ShareError(f"The document has no link on {', '.join(hosts)} ({len(links)} other links).")
    if len({h.rstrip("/") for h in hits}) > 1:
        print(f"  Note: the document has {len(hits)} links on {', '.join(hosts)}; using the first.")
    return hits[0]


def page_links(html: str, base: str) -> list[str]:
    """Every href on a web page, made absolute, in page order, without repeats."""
    seen, out = set(), []
    for groups in _HREF.findall(html):
        href = unescape(next(g for g in groups if g) if any(groups) else "").strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        link = urljoin(base, href)
        if link not in seen:
            seen.add(link)
            out.append(link)
    return out


def _fetch_page(url: str) -> str:
    from trove.http import API_TIMEOUT, BROWSER_HEADERS, new_session

    with new_session().get(url, headers=BROWSER_HEADERS, timeout=API_TIMEOUT, stream=True) as response:
        response.raise_for_status()
        if "html" not in response.headers.get("Content-Type", "").lower():
            raise ShareError(f"{urlparse(url).netloc} isn't a share or a web page.")
        data = bytearray()
        for chunk in response.iter_content(65536):
            data += chunk
            if len(data) > MAX_PAGE_BYTES:
                raise ShareError("The page is too big to be a landing page.")
        return bytes(data).decode(response.encoding or "utf-8", "replace")


def link_in_document(url: str, hosts: list[str]) -> str:
    """Follow ``url`` to a one-file share or a web page, read it, and return its link
    on ``hosts``."""
    from trove.http import resolve_shortener
    from trove.providers import _safe_url, is_provider_url, open_url

    target = resolve_shortener(url, is_provider_url, landing_page=True)
    if not is_provider_url(target):
        # A landing page has no #fragment of its own, so reading it loses nothing.
        link = pick_link(page_links(_fetch_page(target), target), hosts)
        print(f"  Page {_safe_url(target)} links to {_safe_url(link)}")
        return link
    share = open_url(target)
    try:
        listing = share.list()
        if len(listing.files) != 1:
            raise ShareError(f"{_safe_url(target)} should hold one document, not {len(listing.files)} files.")
        file = listing.files[0]
        if file.size and file.size > MAX_DOCUMENT_BYTES:
            raise ShareError(f"The document is {file.size} bytes, too big to be a landing page.")
        data = bytearray()
        for chunk in share.open_transfer(file).chunks:
            data += chunk
            if len(data) > MAX_DOCUMENT_BYTES:
                raise ShareError("The document is too big to be a landing page.")
    finally:
        share.close()
    if not bytes(data[:1024]).lstrip().startswith(b"%PDF"):
        raise ShareError(f"The document ({file.path}) isn't a PDF.")
    link = pick_link(pdf_links(bytes(data)), hosts)
    print(f"  Document {file.path} links to {_safe_url(link)}")
    return link
