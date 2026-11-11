"""Test doubles. All data is synthetic."""

from __future__ import annotations

import posixpath
import time

import pytest

from trove.errors import PermanentTransferError
from trove.fs import FileStat, LocalFS
from trove.model import Listing, RemoteFile, RemoteFolder, Share, Transfer


class FakeShare(Share):
    provider_name = "fake"
    manifest_name = ".fake-manifest.json"
    verify_kind = "etag"
    can_resume = True

    def __init__(self, contents: dict[str, bytes] | None = None):
        self.contents = contents or {}
        self.transfers: list[tuple[str, int]] = []
        self.fail_after: dict[str, int] = {}
        self.ignore_range = False
        self.missing: set[str] = set()

    @property
    def identity(self) -> str:
        return "fake"

    def describe(self):
        return []

    def probe(self):
        pass

    def list(self, subpath=""):
        raise NotImplementedError

    def open_transfer(self, file, offset=0):
        self.transfers.append((file.path, offset))
        if file.path in self.missing:
            raise PermanentTransferError("HTTP 404")
        data = self.contents[file.path]
        start = 0 if self.ignore_range else offset
        limit = self.fail_after.pop(file.path, None)

        def chunks():
            sent = start
            while sent < len(data):
                if limit is not None and sent >= limit:
                    raise ConnectionError("dropped")
                end = min(sent + 4, limit if limit is not None else len(data))
                yield data[sent:end]
                sent = end

        return Transfer(chunks(), start)


class MemFile:
    def __init__(self, fs, path, mode):
        self.fs, self.path, self.mode = fs, path, mode
        self.data = bytearray(fs.files[path][0] if "a" in mode else b"")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fs.files[self.path] = (bytes(self.data), time.time())

    def write(self, chunk):
        self.data += chunk


class MemReader:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def read(self, size=-1):
        size = len(self.data) if size < 0 else size
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk


class MemFS:
    """A case-sensitive POSIX tree, behaving like an SFTP destination."""

    is_remote = True

    def __init__(self):
        self.files: dict[str, tuple[bytes, float]] = {}
        self.dirs: dict[str, float] = {"/": 1.0}
        self.calls = 0

    def describe(self, path):
        return f"mem://{path}"

    def join(self, *parts):
        return posixpath.join(*parts)

    def stat(self, path):
        self.calls += 1
        if path in self.files:
            data, mtime = self.files[path]
            return FileStat(len(data), mtime)
        if path in self.dirs:
            return FileStat(0, self.dirs[path], True)
        return None

    def isdir(self, path):
        return path in self.dirs

    def list_files(self, path):
        self.calls += 1
        return {
            posixpath.basename(p): FileStat(len(d), m)
            for p, (d, m) in self.files.items()
            if posixpath.dirname(p) == path
        }

    def walk(self, top):
        dirs = sorted(posixpath.basename(d) for d in self.dirs if d != "/" and posixpath.dirname(d) == top)
        files = sorted(posixpath.basename(f) for f in self.files if posixpath.dirname(f) == top)
        yield top, dirs, files
        for d in dirs:
            yield from self.walk(posixpath.join(top, d))

    def makedirs(self, path):
        parts = [p for p in path.split("/") if p]
        cur = "/"
        for part in parts:
            cur = posixpath.join(cur, part)
            self.dirs.setdefault(cur, time.time())

    def open(self, path, mode="rb"):
        if "r" in mode:
            if path not in self.files:
                raise FileNotFoundError(path)
            return MemReader(self.files[path][0])
        return MemFile(self, path, mode)

    def replace(self, src, dst):
        if src in self.dirs:
            for key in [k for k in self.dirs if k == src or k.startswith(src + "/")]:
                self.dirs[dst + key[len(src) :]] = self.dirs.pop(key)
            for key in [k for k in self.files if k.startswith(src + "/")]:
                self.files[dst + key[len(src) :]] = self.files.pop(key)
            return
        self.files[dst] = self.files.pop(src)

    def remove(self, path):
        del self.files[path]

    def rmdir(self, path):
        if any(posixpath.dirname(k) == path for k in [*self.files, *self.dirs]):
            raise OSError("not empty")
        del self.dirs[path]

    def set_mtime(self, path, mtime):
        if path in self.files:
            self.files[path] = (self.files[path][0], mtime)
        else:
            self.dirs[path] = mtime

    def close(self):
        pass


@pytest.fixture(params=["local", "sftp-like"])
def dest(request, tmp_path):
    """Run a test against a real local folder and a POSIX, case-sensitive tree."""
    if request.param == "local":
        return LocalFS(), str(tmp_path)
    fs = MemFS()
    fs.makedirs("/out")
    return fs, "/out"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def no_mediafire_account(monkeypatch):
    """Tests never read a real MediaFire login from the environment or the keyring."""
    from trove.providers import mediafire

    monkeypatch.setattr(mediafire, "find_credentials", lambda: None)


def make_listing(share: FakeShare, mtime: float = 1_600_000_000.0, etags: dict | None = None, folders=()):
    files = [
        RemoteFile(path, len(data), mtime, (etags or {}).get(path))
        for path, data in share.contents.items()
    ]
    wanted = set(folders)
    for path in share.contents:
        parent = path.rpartition("/")[0]
        while parent:
            wanted.add(parent)
            parent = parent.rpartition("/")[0]
    return Listing(files, [RemoteFolder(p, mtime) for p in sorted(wanted)], share_name="Share", root_mtime=mtime)
