"""What providers that drive a real browser share (the ``browser`` extra).

Some sites only hand files to a browser that ran their own page: ufile behind a
reCAPTCHA, workupload behind an ad. Those providers drive a visible Chromium through
Playwright and leave anything meant for a person (a reCAPTCHA, an ad) to the person
at the screen. So they need a desktop, and can't run on a headless server.
"""

from __future__ import annotations

import os
import sys

from platformdirs import user_data_dir

from trove.errors import ShareError

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def sync_playwright():
    try:
        from playwright.sync_api import sync_playwright as start
    except ImportError:
        raise ShareError(
            "This site needs a browser: pip install 'trove-downloader[browser]' && python -m playwright install chromium"
        ) from None
    return start


def has_display() -> bool:
    """A visible browser needs a desktop: always on Windows and macOS, and a display
    server elsewhere."""
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def require_display(why: str) -> None:
    if not has_display():
        raise ShareError(f"{why}, so it needs a visible browser and can't run on a headless server.")


def profile_dir(name: str) -> str:
    """A persistent browser profile per site, so cookies and trust carry over between runs."""
    path = os.path.join(user_data_dir("trove-downloader", appauthor=False), f"{name}-browser")
    os.makedirs(path, exist_ok=True)
    return path


def apply_stealth(page) -> None:
    """playwright-stealth is optional, and its API changed in 2.0."""
    try:
        from playwright_stealth import Stealth

        Stealth().apply_stealth_sync(page)
        return
    except ImportError:
        pass
    except Exception:
        return
    try:
        from playwright_stealth import stealth_sync

        stealth_sync(page)
    except Exception:
        pass
