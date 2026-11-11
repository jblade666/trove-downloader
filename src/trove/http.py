"""HTTP helpers shared by providers."""

from __future__ import annotations

import time
from urllib.parse import urljoin, urlparse

import requests

from trove.errors import PermanentTransferError, StalePartial
from trove.model import Transfer

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Some shorteners sit behind a bot check that needs the full browser header set.
BROWSER_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

API_TIMEOUT = (15, 60)  # (connect, read) seconds
API_RETRIES = 4
CHUNK_SIZE = 1024 * 1024
_REDIRECT_CODES = {301, 302, 303, 307, 308}


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA})
    return session


def request_with_retry(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """Send a request, retrying connection errors, timeouts, 429 and 5xx.

    Other 4xx responses won't change on retry, so they raise immediately.
    """
    kwargs.setdefault("timeout", API_TIMEOUT)
    for attempt in range(API_RETRIES):
        try:
            response = session.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status is not None and status != 429 and status < 500:
                raise
            error: Exception = e
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            error = e
        if attempt == API_RETRIES - 1:
            raise error
        wait = 5 * (2**attempt)
        print(
            f"  Network error ({error.__class__.__name__}); "
            f"retry {attempt + 1}/{API_RETRIES - 1} in {wait}s...",
            flush=True,
        )
        time.sleep(wait)
    raise AssertionError("unreachable")


def open_http_transfer(session: requests.Session, url: str, offset: int = 0, **kwargs) -> Transfer:
    """Start a GET, resuming from ``offset`` when the server honours Range."""
    headers = dict(kwargs.pop("headers", None) or {})
    if offset:
        headers["Range"] = f"bytes={offset}-"
    response = session.get(url, headers=headers, stream=True, timeout=API_TIMEOUT, **kwargs)

    if offset and response.status_code == 416:
        response.close()
        raise StalePartial("the server rejected the resume offset")
    status = response.status_code
    if status >= 400:
        response.close()
        if status == 429 or status >= 500:
            response.raise_for_status()  # HTTPError is an OSError, so it is retried
        raise PermanentTransferError(f"HTTP {status} {response.reason}")

    # A 200 answer to a Range request means the whole file is coming.
    start = offset if status == 206 else 0

    def chunks():
        try:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    yield chunk
        finally:
            response.close()

    return Transfer(chunks=chunks(), offset=start)


def resolve_shortener(
    url: str, is_final, max_hops: int = 8, session: requests.Session | None = None, landing_page: bool = False
) -> str:
    """Follow a link shortener to the first URL ``is_final`` accepts.

    Redirects are walked by hand and the final URL is never fetched: some links
    keep a decryption key in the #fragment, which automatic redirects would lose.
    With ``landing_page``, a page that answers instead of redirecting ends the walk
    too, and its URL is returned for the caller to read.
    """
    # Before adding a scheme: a bare share ID would otherwise be fetched as a hostname.
    if is_final(url):
        return url
    session = session or requests.Session()
    current = url if "://" in url else "https://" + url.strip()
    for _ in range(max_hops):
        if is_final(current):
            return current
        response = session.get(
            current,
            headers=BROWSER_HEADERS,
            allow_redirects=False,
            stream=True,
            timeout=API_TIMEOUT,
        )
        response.close()
        host = urlparse(current).netloc
        if landing_page and response.status_code == 200:
            return current
        if response.status_code not in _REDIRECT_CODES:
            raise ValueError(f"{host} answered HTTP {response.status_code} instead of redirecting")
        location = response.headers.get("Location")
        if not location:
            raise ValueError(f"{host} sent HTTP {response.status_code} with no Location")
        current = urljoin(current, location)
    raise ValueError("Too many redirects")
