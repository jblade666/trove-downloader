"""One `trove sync` at a time.

Two passes over the same sites would download the same files at once, race on the
state file, and with SFTP, open a second connection that the server may penalize. A
scheduled run that starts while the last one is still going must step aside.

The lock is an OS file lock held for the life of the process, so it's released
however the process ends, crash or kill included, and there's no stale lock file to
clean up. The file records who holds it, for the message.
"""

from __future__ import annotations

import os
import sys
import time

from trove.errors import TroveError


class AlreadyRunning(TroveError):
    """Another `trove sync` holds the lock."""


class RunLock:
    def __init__(self, path: str):
        self.path = path
        self._fp = None

    def __enter__(self) -> "RunLock":
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fp = open(self.path, "a+", encoding="utf-8")
        try:
            _lock(fp)
        except OSError:
            try:
                fp.seek(0)
                holder = fp.read(200).strip()
            except OSError:
                holder = ""
            fp.close()
            holder = holder or "another process"
            raise AlreadyRunning(f"Another trove sync is running ({holder}); not starting a second one.") from None
        fp.seek(0)
        fp.truncate()
        fp.write(f"pid {os.getpid()}, started {time.strftime('%Y-%m-%d %H:%M:%S')}")
        fp.flush()
        self._fp = fp
        return self

    def __exit__(self, *exc) -> None:
        if self._fp is not None:
            try:
                _unlock(self._fp)
            finally:
                self._fp.close()
                self._fp = None


if sys.platform == "win32":
    import msvcrt

    # Windows locks byte ranges against reading too, so lock one far past the text,
    # which stays readable for the message.
    LOCK_OFFSET = 1 << 20

    def _lock(fp) -> None:
        fp.seek(LOCK_OFFSET)
        msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fp) -> None:
        fp.seek(LOCK_OFFSET)
        try:
            msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _lock(fp) -> None:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fp) -> None:
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
