"""One filesystem interface for a local path or an SFTP server.

Paths belong to their filesystem: native paths for ``LocalFS``, absolute POSIX
paths for ``SFTPFS``. Build them with ``fs.join``, never ``os.path``.

Over SFTP every call is a network round trip, so callers list a directory once
rather than stat each file. Connections can drop during long transfers, so
``SFTPFS`` reconnects on the next call and callers treat ``RETRYABLE_ERRORS`` as
retryable. Keep one connection per server: OpenSSH's PerSourcePenalties can
refuse an address that reconnects rapidly.
"""

from __future__ import annotations

import os
import posixpath
import stat as stat_mod
import sys
from dataclasses import dataclass, field

IO_TIMEOUT = 120  # seconds an SFTP read or write may stall before it errors out


@dataclass
class FileStat:
    size: int
    mtime: float
    is_dir: bool = False


def _retryable_errors() -> tuple:
    errs: list = [OSError, EOFError, ConnectionError]
    try:
        import paramiko

        errs.append(paramiko.SSHException)
    except ImportError:
        pass
    return tuple(errs)


RETRYABLE_ERRORS = _retryable_errors()


class LocalFS:
    """Thin wrapper over os and os.path with the same API as SFTPFS."""

    is_remote = False
    is_mount = False

    def describe(self, path: str) -> str:
        return path

    def canonical(self, path: str) -> str:
        """How the state file records a folder: the same whichever way it's reached."""
        return path

    def join(self, *parts: str) -> str:
        return os.path.join(*parts)

    def stat(self, path: str) -> FileStat | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return FileStat(st.st_size, st.st_mtime, stat_mod.S_ISDIR(st.st_mode))

    def isdir(self, path: str) -> bool:
        return os.path.isdir(path)

    def list_files(self, path: str) -> dict[str, FileStat]:
        """name -> FileStat for the regular files directly in ``path``."""
        out: dict[str, FileStat] = {}
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if entry.is_file():
                        st = entry.stat()
                        out[entry.name] = FileStat(st.st_size, st.st_mtime)
        except FileNotFoundError:
            pass
        return out

    def walk(self, top: str):
        """Like os.walk: top-down, and callers may prune ``dirnames`` in place."""
        yield from os.walk(top)

    def walk_stats(self, top: str):
        """Like walk, but with ``{name: FileStat}`` for the regular files."""
        dirs, files = [], {}
        try:
            with os.scandir(top) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(entry.name)
                    elif entry.is_file():
                        st = entry.stat()
                        files[entry.name] = FileStat(st.st_size, st.st_mtime)
        except FileNotFoundError:
            return
        yield top, dirs, files
        for d in dirs:
            yield from self.walk_stats(os.path.join(top, d))

    def makedirs(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def open(self, path: str, mode: str = "rb"):
        return open(path, mode)

    def replace(self, src: str, dst: str) -> None:
        os.replace(src, dst)

    def remove(self, path: str) -> None:
        os.remove(path)

    def rmdir(self, path: str) -> None:
        os.rmdir(path)

    def set_mtime(self, path: str, mtime: float) -> None:
        os.utime(path, (mtime, mtime))

    def close(self) -> None:
        pass


@dataclass
class SFTPSettings:
    """Everything needed to reach a server. Nothing here has a built-in default
    except the standard SSH port and the local username."""

    host: str
    port: int = 22
    username: str | None = None
    identity_file: str | None = None
    password_file: str | None = None
    keyring_service: str = "trove-downloader"
    known_hosts: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.username}@{self.host}:{self.port}"


PASSWORD_ENV = "TROVE_SFTP_PASSWORD"


def keyring_user(settings: SFTPSettings) -> str:
    return settings.label


def find_password(settings: SFTPSettings) -> str | None:
    """Password lookup: environment, then password file, then the OS keyring."""
    pw = os.environ.get(PASSWORD_ENV)
    if pw:
        return pw
    if settings.password_file:
        with open(os.path.expanduser(settings.password_file), encoding="utf-8") as fp:
            return fp.read().strip()
    try:
        import keyring

        return keyring.get_password(settings.keyring_service, keyring_user(settings))
    except Exception:
        return None


class MountFS(LocalFS):
    """A local mount (a mapped drive, say) of an SFTP destination's folder, which is
    much faster to write through. Paths are local; the state file still records the
    SFTP form, so a folder reads the same whichever way a run reached it."""

    is_mount = True

    def __init__(self, local_root: str, remote_root: str):
        self.local_root = os.path.normpath(local_root)
        self.remote_root = remote_root.rstrip("/")  # sftp://user@host:port/path

    def canonical(self, path: str) -> str:
        rel = os.path.relpath(os.path.normpath(path), self.local_root).replace(os.sep, "/")
        return self.remote_root if rel == "." else f"{self.remote_root}/{rel}"


class SFTPFS:
    """SFTP-backed filesystem. Paths are absolute POSIX paths on the server."""

    is_remote = True

    def __init__(self, settings: SFTPSettings):
        self.settings = settings
        self._password: str | None = None
        self._client = None
        self._sftp = None

    def describe(self, path: str) -> str:
        return f"sftp://{self.settings.label}{path}"

    def canonical(self, path: str) -> str:
        return self.describe(path)

    def _conn(self):
        transport = self._client.get_transport() if self._client else None
        if self._sftp is None or transport is None or not transport.is_active():
            self._connect()
        return self._sftp

    def _connect(self) -> None:
        import paramiko

        from trove.errors import ConfigError

        self.close()
        s = self.settings
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        known_hosts = s.known_hosts and os.path.expanduser(s.known_hosts)
        if known_hosts:
            os.makedirs(os.path.dirname(os.path.abspath(known_hosts)), exist_ok=True)
            if os.path.exists(known_hosts):
                client.load_host_keys(known_hosts)
        client.set_missing_host_key_policy(_TofuPolicy(known_hosts))

        # One connection that tries every method. Probing with keys and then
        # reconnecting with a password can trigger the server's reconnect penalty.
        if self._password is None:
            self._password = find_password(s)
        try:
            client.connect(
                hostname=s.host,
                port=s.port,
                username=s.username,
                password=self._password,
                key_filename=s.identity_file and os.path.expanduser(s.identity_file),
                allow_agent=True,
                look_for_keys=True,
                timeout=30,
                banner_timeout=30,
                auth_timeout=30,
            )
        except paramiko.BadHostKeyException:
            raise
        except paramiko.SSHException as e:
            if not (
                isinstance(e, paramiko.AuthenticationException)
                or "authentication" in str(e).lower()
            ):
                raise
            hint = ""
            if not self._password:
                hint = (
                    f" (no password found: set ${PASSWORD_ENV}, set password_file "
                    f"in the destination, or run 'trove set-password')"
                )
            raise ConfigError(f"SFTP auth failed for {s.label}: {e}{hint}") from e

        client.get_transport().set_keepalive(30)
        self._client = client
        self._sftp = client.open_sftp()
        # Without this, a server that stops answering mid-transfer blocks forever.
        # The timeout raises an OSError, and the next call reconnects.
        self._sftp.get_channel().settimeout(IO_TIMEOUT)

    def close(self) -> None:
        for obj in (self._sftp, self._client):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._sftp = self._client = None

    def join(self, *parts: str) -> str:
        return posixpath.join(*parts)

    def stat(self, path: str) -> FileStat | None:
        try:
            st = self._conn().stat(path)
        except FileNotFoundError:
            return None
        return FileStat(
            st.st_size or 0, float(st.st_mtime or 0), stat_mod.S_ISDIR(st.st_mode or 0)
        )

    def isdir(self, path: str) -> bool:
        st = self.stat(path)
        return bool(st and st.is_dir)

    def _listdir_attr(self, path: str):
        try:
            return self._conn().listdir_attr(path)
        except FileNotFoundError:
            return []

    def list_files(self, path: str) -> dict[str, FileStat]:
        return {
            a.filename: FileStat(a.st_size or 0, float(a.st_mtime or 0))
            for a in self._listdir_attr(path)
            if stat_mod.S_ISREG(a.st_mode or 0)
        }

    def walk(self, top: str):
        dirs, files = [], []
        for a in self._listdir_attr(top):
            (dirs if stat_mod.S_ISDIR(a.st_mode or 0) else files).append(a.filename)
        yield top, dirs, files
        for d in dirs:
            yield from self.walk(posixpath.join(top, d))

    def walk_stats(self, top: str):
        """Like walk, but with ``{name: FileStat}`` for the regular files, from the
        same listing, so it costs no extra round trips."""
        dirs, files = [], {}
        for a in self._listdir_attr(top):
            if stat_mod.S_ISDIR(a.st_mode or 0):
                dirs.append(a.filename)
            elif stat_mod.S_ISREG(a.st_mode or 0):
                files[a.filename] = FileStat(a.st_size or 0, float(a.st_mtime or 0))
        yield top, dirs, files
        for d in dirs:
            yield from self.walk_stats(posixpath.join(top, d))

    def makedirs(self, path: str) -> None:
        sftp = self._conn()
        cur = "/" if path.startswith("/") else ""
        for part in [p for p in path.split("/") if p]:
            cur = posixpath.join(cur, part) if cur else part
            try:
                sftp.stat(cur)
            except FileNotFoundError:
                sftp.mkdir(cur)

    def open(self, path: str, mode: str = "rb"):
        f = self._conn().open(path, mode, bufsize=1024 * 1024)
        if "r" in mode:
            f.prefetch()  # pipelined reads help a lot over high-latency links
        else:
            f.set_pipelined(True)  # don't wait for an ack per write
        return f

    def replace(self, src: str, dst: str) -> None:
        sftp = self._conn()
        try:
            sftp.posix_rename(src, dst)  # atomic overwrite (OpenSSH extension)
        except OSError:
            # A plain SFTP rename refuses to overwrite.
            try:
                sftp.remove(dst)
            except FileNotFoundError:
                pass
            sftp.rename(src, dst)

    def remove(self, path: str) -> None:
        self._conn().remove(path)

    def rmdir(self, path: str) -> None:
        self._conn().rmdir(path)

    def set_mtime(self, path: str, mtime: float) -> None:
        self._conn().utime(path, (mtime, mtime))


class _TofuPolicy:
    """Trust on first use: accept an unknown host key, save it, and print it.

    A later mismatch aborts with paramiko's BadHostKeyException.
    """

    def __init__(self, known_hosts: str | None):
        self.known_hosts = known_hosts

    def missing_host_key(self, client, hostname, key):
        fingerprint = key.fingerprint if hasattr(key, "fingerprint") else key.get_base64()[:32]
        print(
            f"  New SSH host key for {hostname}: {key.get_name()} {fingerprint}",
            file=sys.stderr,
        )
        client.get_host_keys().add(hostname, key.get_name(), key)
        if self.known_hosts:
            client.save_host_keys(self.known_hosts)
