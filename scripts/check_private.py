#!/usr/bin/env python3
"""Refuse a commit whose staged changes contain personal strings.

The list lives outside the repo, one case-insensitive string per line, so the check
itself reveals nothing. Its location is $TROVE_PRIVATE_STRINGS, or
private-strings.txt next to the config file.

Install as a local hook:

    printf '#!/bin/sh\\nexec python scripts/check_private.py\\n' > .git/hooks/pre-commit
"""

import os
import subprocess
import sys

from platformdirs import user_config_dir


def main() -> int:
    path = os.environ.get("TROVE_PRIVATE_STRINGS") or os.path.join(
        user_config_dir("trove-downloader", appauthor=False), "private-strings.txt"
    )
    try:
        with open(path, encoding="utf-8") as fp:
            strings = [s.strip() for s in fp if s.strip() and not s.startswith("#")]
    except FileNotFoundError:
        print(f"check_private: no list at {path}; refusing, so the check can't be skipped by accident.")
        return 1

    diff = subprocess.run(
        ["git", "diff", "--cached", "--unified=0", "--no-color"],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", "replace")

    current = "?"
    failures = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[4:].removeprefix("b/")
            lowered_name = current.lower()
            for s in strings:
                if s.lower() in lowered_name:
                    print(f"check_private: file name {current} contains a private string (entry {strings.index(s) + 1})")
                    failures += 1
            continue
        if not line.startswith("+"):
            continue
        lowered = line.lower()
        for s in strings:
            if s.lower() in lowered:
                # Report the entry number rather than the string, so hook output is safe to share.
                print(f"check_private: {current} adds private string (entry {strings.index(s) + 1})")
                failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
