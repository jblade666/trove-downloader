"""Map uploader-controlled remote names to safe destination names.

Remote names can contain anything, including ``..``. Sanitizing runs on every
platform and for every destination, because a tree written over SFTP is often
browsed later from Windows, and the name on disk must not depend on which machine
ran the sync.

Providers choose how a colon is written, because each one must keep matching the
files its earlier versions already wrote.
"""

import re

_RESERVED_CHARS = re.compile(r'[?*"<>|\\\x00-\x1f]')

_RESERVED_DEVICES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


# What MEGAcmd writes as %xx: the characters Windows forbids in a name.
_PERCENT_CHARS = re.compile(r'[\\:*?"<>|\x00-\x1f]')


def safe_component(name: str, colon: str = "dash") -> str:
    """Return a name that is legal on every filesystem we write to.

    ``colon="dash"`` turns ``"A: B"`` into ``"A - B"``; ``colon="underscore"``
    turns it into ``"A_ B"``. ``colon="percent"`` writes every reserved character
    as ``%xx`` and drops leading spaces too, the way MEGAcmd does. Stripping
    trailing dots and spaces also turns ``.`` and ``..`` into ``_``, so a remote
    path can't leave the output directory.
    """
    if colon == "percent":
        name = _PERCENT_CHARS.sub(lambda m: f"%{ord(m.group()):02x}", name).lstrip(" ")
    elif colon == "dash":
        name = name.replace(": ", " - ").replace(":", "-")
    else:
        name = name.replace(":", "_")
    name = _RESERVED_CHARS.sub("_", name).replace("/", "_")
    name = name.rstrip(". ")
    if not name:
        return "_"
    if name.split(".")[0].upper() in _RESERVED_DEVICES:
        name = "_" + name
    return name


def safe_path(rel_path: str, colon: str = "dash") -> str:
    """Sanitize each part of a '/'-separated remote path."""
    return "/".join(safe_component(p, colon) for p in rel_path.split("/") if p)
