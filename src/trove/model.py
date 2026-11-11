"""What a provider hands to the sync engine."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator


@dataclass
class RemoteFile:
    """``path`` is relative to the share root. ``key`` is the provider's own ID for
    the file, for sites that fetch by ID rather than by path. The engine fills
    ``local`` with the safe path relative to the output folder.
    """

    path: str
    size: int | None = None
    mtime: float | None = None
    etag: str | None = None
    key: str | None = None
    local: str = ""


@dataclass
class RemoteFolder:
    path: str
    mtime: float | None = None
    local: str = ""


@dataclass
class Listing:
    """``share_name`` names the whole share. ``root_mtime`` dates the listed root,
    which is the requested subfolder when one was asked for."""

    files: list[RemoteFile]
    folders: list[RemoteFolder]
    share_name: str | None = None
    root_mtime: float | None = None


@dataclass
class Transfer:
    """An open download. ``offset`` is where the stream really starts, which is 0
    when the server ignored a resume request."""

    chunks: Iterator[bytes]
    offset: int = 0


class Share(ABC):
    """One opened share. All state lives on the instance, so a batch can't leak a
    password or token from one share into the next."""

    provider_name: str = ""
    # How colons are written locally. See trove.naming.
    colon: str = "dash"
    # Name of the state file kept in the output folder, if the provider has one.
    manifest_name: str | None = None
    # "etag": --verify compares recorded server tags. "md5" or "blake2b-240":
    # RemoteFile.etag is that content hash, so --verify reads files back. See
    # trove.engine.CONTENT_HASHES. None: nothing to verify with.
    verify_kind: str | None = None
    can_resume: bool = False
    # For a flat share where uploads can share a name: each RemoteFile.key keeps the
    # local name it was first given, recorded in the manifest, and a later upload
    # with a clashing name gets " [key]" added. Needs manifest_name.
    stable_names: bool = False
    # Match existing files and folders loosely: ignoring case, Unicode normalization,
    # %xx escapes and leading or trailing spaces and dots, and keep their names on
    # disk. For trees written by tools that changed names on the way.
    loose_names: bool = False
    # Local names starting with these are another tool's leftovers, never orphans.
    junk_prefixes: tuple[str, ...] = ()

    @property
    @abstractmethod
    def identity(self) -> str:
        """A stable ID, used to tell whether two links name the same share."""

    @abstractmethod
    def describe(self) -> list[str]:
        """Lines safe to print. Must not contain keys or passwords."""

    @abstractmethod
    def probe(self) -> None:
        """One cheap request that raises if the share is unusable."""

    @abstractmethod
    def list(self, subpath: str = "") -> Listing:
        """List the share, or one subfolder, recursively."""

    def close(self) -> None:
        """Release anything held between transfers, such as a browser. Called once
        the share is done, however it ended."""

    @abstractmethod
    def open_transfer(self, file: RemoteFile, offset: int = 0) -> Transfer:
        """Start a download. Called only when a file really needs fetching, so a
        dry run or an all-skip run never reaches it."""
