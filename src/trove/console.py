"""Console output helpers."""

import shutil
import sys


def setup_console() -> None:
    """Use UTF-8 and line buffering.

    Windows consoles default to cp1252, which can't print many file names, and
    without line buffering a redirected log only appears when the process exits.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError):
            pass


def fmt_size(size: float | None) -> str:
    if size is None:
        return "unknown size"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


class Progress:
    """A single transient progress row, drawn only on a real terminal.

    The row is rewritten with a carriage return, so it is cut to the terminal
    width (a wrapped row can't be rewritten) and must be cleared before any other
    output.
    """

    def __init__(self) -> None:
        self._active = False
        self._enabled = sys.stdout.isatty()

    def update(self, text: str) -> None:
        if not self._enabled:
            return
        width = shutil.get_terminal_size((80, 24)).columns - 1
        sys.stdout.write("\r" + text[:width].ljust(width))
        sys.stdout.flush()
        self._active = True

    def clear(self) -> None:
        if self._active:
            width = shutil.get_terminal_size((80, 24)).columns - 1
            sys.stdout.write("\r" + " " * width + "\r")
            sys.stdout.flush()
            self._active = False
