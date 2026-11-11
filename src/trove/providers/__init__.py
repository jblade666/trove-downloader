"""Provider registry and link handling."""

from __future__ import annotations

import re

from trove.errors import ShareError
from trove.http import resolve_shortener
from trove.model import Share
from trove.providers import allsync, gofile, koofr, kregfile, mediafire, mega, ufile, workupload, wormhole, yandex

# Order matters: the first provider whose matches() accepts a URL wins. Providers tied
# to specific hosts come first. kregfile and allsync accept any host, by path shape
# (/r/ROOM and /s/TOKEN), so they come last.
PROVIDERS = {
    "gofile": gofile,
    "koofr": koofr,
    "mediafire": mediafire,
    "mega": mega,
    "ufile": ufile,
    "workupload": workupload,
    "wormhole": wormhole,
    "yandex": yandex,
    "kregfile": kregfile,
    "allsync": allsync,
}


def is_provider_url(url: str) -> bool:
    return any(p.matches(url) for p in PROVIDERS.values())


def bare_links(text: str, provider: str | None = None) -> list[str]:
    """The full links a bare ID (``Ima81tmu``, ``ID#KEY``) could stand for: the named
    provider's reading, or without one, every provider's. Detection never takes a bare
    ID on its own, since it could be any host's; ``get`` uses these to find a known site."""
    text = text.strip()
    if provider:
        modules = [PROVIDERS[provider]] if provider in PROVIDERS else []
    else:
        modules = list(PROVIDERS.values())
    links = []
    for module in modules:
        to_url = getattr(module, "bare_url", None)
        link = to_url(text) if to_url and "://" not in text and "." not in text.split("/")[0] else None
        if link:
            links.append(link)
    return links


def open_url(url: str, password: str = "", provider: str | None = None) -> Share:
    """Open a direct share link."""
    if provider:
        module = PROVIDERS.get(provider)
        if module is None:
            raise ShareError(f"Unknown provider {provider!r} (known: {', '.join(PROVIDERS)})")
        if not module.matches(url):
            url = next(iter(bare_links(url, provider)), url)
        return module.open_share(url, password)
    for module in PROVIDERS.values():
        if module.matches(url):
            return module.open_share(url, password)
    raise ShareError(f"No provider recognizes this link: {_safe_url(url)}")


def _safe_url(url: str) -> str:
    """A link without its #fragment, which can hold a decryption key, or a password."""
    return re.sub(r"([?&]password=)[^&]*", r"\1***", url.split("#", 1)[0])


def select_share(
    urls: list[str], password: str = "", provider: str | None = None, link_in_document: list[str] | None = None
) -> Share:
    """Open the first usable link, trying fallbacks in order.

    With ``link_in_document``, the first link leads to a document whose link on one
    of those hosts is the real one (see trove.documents). Shorteners are resolved first. Every link with a fallback after it is probed,
    so a dead share falls through to the next. The last link isn't probed, because
    the listing reports its errors anyway.
    """
    all_gone = True
    for i, url in enumerate(urls):
        print(f"{'Link' if i == 0 else 'Fallback'}: {_safe_url(url)}")
        try:
            if i == 0 and link_in_document:
                from trove.documents import link_in_document as read_document

                url = read_document(url, link_in_document)
            is_final = (lambda _u: True) if provider else is_provider_url
            resolved = resolve_shortener(url, is_final)
            if resolved != url:
                print(f"  -> {_safe_url(resolved)}")
            share = open_url(resolved, password, provider)
            if i < len(urls) - 1:
                share.probe()
        except Exception as e:
            print(f"  Unusable: {e}")
            all_gone = all_gone and is_gone_error(e)
            continue
        _warn_stale_fallbacks(share, urls[i + 1 :], password, provider)
        return share
    raise ShareError("No usable link.", gone=all_gone)


def is_gone_error(error: Exception) -> bool:
    """True when a failure says the link is dead rather than temporarily failing.

    A ValueError comes from a shortener that no longer redirects anywhere useful.
    """
    if isinstance(error, ShareError):
        return error.gone
    if isinstance(error, ValueError):
        return True
    response = getattr(error, "response", None)
    return response is not None and response.status_code in (404, 410)


def _warn_stale_fallbacks(share: Share, fallbacks: list[str], password: str, provider: str | None) -> None:
    """Warn when an unused direct fallback names a different share, which means
    the shortener has been repointed and the fallback is out of date."""
    for url in fallbacks:
        if not (provider or is_provider_url(url)):
            continue
        try:
            other = open_url(url, password, provider)
        except Exception:
            continue
        if other.identity != share.identity:
            print(f"  WARNING: fallback {_safe_url(url)} points at a different share; update it.")
