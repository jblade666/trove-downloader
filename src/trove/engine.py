"""The sync engine: everything between a provider's listing and the destination.

Rules worth keeping:

* All destination I/O goes through ``fs``. Build paths with ``fs.join``.
* List each destination directory once. Over SFTP every call is a round trip.
* Only write a date that differs by at least a second, because SFTP keeps whole
  seconds. An up-to-date tree must report 0 dates changed.
* A dry run writes nothing, not even a date or a manifest.
* ``Share.open_transfer`` is called only when a file really needs fetching.
* The remote ``path`` is never sanitized. Only ``local`` is.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field

from trove.console import Progress, fmt_size
from trove.errors import AbortRun, CorruptDownload, ManifestError, PermanentTransferError, ShareError, StalePartial
from trove.fs import RETRYABLE_ERRORS, FileStat
from trove.model import Listing, RemoteFile, RemoteFolder, Share
from trove.naming import safe_component, safe_path
from trove.state import ShareInfo

PART_SUFFIX = ".part"
MANIFEST_SAVE_INTERVAL = 30  # seconds; a big manifest is costly to rewrite over SFTP
TRANSFER_ATTEMPTS = 3
IGNORED_NAMES = {"thumbs.db", "ehthumbs.db", "desktop.ini", ".ds_store"}
DEFAULT_DELETE_MAX_PCT = 25.0


@dataclass
class SyncOptions:
    dry_run: bool = False
    dates_only: bool = False
    verify: bool = False
    seed_manifest: bool = False
    no_orphans: bool = False
    delete_orphans: bool = False
    delete_max_pct: float = DEFAULT_DELETE_MAX_PCT
    keep_markers: tuple[str, ...] = ()
    only: tuple[str, ...] = ()
    subpath: str = ""
    # True when the output folder IS the requested subfolder (--dest with --path).
    reroot: bool = False
    rollover: str | None = None
    # Leave the orphan check to the caller, who judges several sites sharing this
    # output folder together (see judge_orphans).
    defer_orphans: bool = False


@dataclass
class SyncResult:
    downloaded: int = 0
    skipped: int = 0
    dates_changed: int = 0
    reverified: int = 0
    errors: int = 0
    date_failures: int = 0
    orphans: list[str] = field(default_factory=list)
    deleted: int = 0
    renamed: int = 0
    would_download: int = 0
    info: ShareInfo | None = None
    claim: "OrphanClaim | None" = None

    @property
    def ok(self) -> bool:
        return self.errors == 0


def _dest(fs, output_dir: str, rel: str) -> str:
    return fs.join(output_dir, *rel.split("/")) if rel else output_dir


def _join_rel(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def _rel_to(path: str, output_dir: str) -> str:
    return path[len(output_dir) :].replace("\\", "/").strip("/")


def assign_local_paths(listing: Listing, share: Share, options: SyncOptions) -> None:
    """Fill in each entry's safe path relative to the output folder.

    Normally an entry keeps its place in the share, so a subfolder sync writes to
    the same place a full sync would. With ``reroot`` the subfolder's contents land
    directly in the output folder instead.
    """
    prefix = options.subpath.strip("/") + "/" if options.subpath.strip("/") else ""

    def local(path: str) -> str:
        if options.reroot and prefix and path.startswith(prefix):
            path = path[len(prefix) :]
        return safe_path(path, share.colon)

    for entry in [*listing.files, *listing.folders]:
        entry.local = local(entry.path)
    listing.folders = [f for f in listing.folders if f.local]


def assign_stable_names(files: list[RemoteFile], share: Share, fs, output_dir: str, dry_run: bool) -> None:
    """Keep each file's local name stable by its key, for ``Share.stable_names``.

    The manifest maps key to local name. A key seen before keeps its name, even if
    another upload now has the same one. A new key whose name was ever given out,
    ignoring case, gets `` [key]`` before the extension. That includes the names of
    uploads that have since expired: their file may still be there, and a new upload
    taking its name would be skipped as already present. Listing order decides who
    keeps the plain name.
    """
    manifest = load_manifest(fs, output_dir, share.manifest_name)
    # The manifest is just a file in the destination, so a name read from it gets the
    # same treatment as a remote one. A slash would leave the folder.
    given = {
        key: safe_component(name.replace("/", "_"), share.colon)
        for key, name in manifest.items()
        if isinstance(name, str) and name
    }
    used = {name.lower(): key for key, name in given.items()}  # lowercased name -> owner
    placed: set[str] = set()
    for f in files:
        if f.key in given:
            f.local = given[f.key]
            placed.add(f.key)

    changed = False
    for f in files:
        if f.key in placed:
            continue
        owner = used.get(f.local.lower())
        if owner is not None and owner != f.key:
            stem, ext = os.path.splitext(f.local)
            f.local = safe_component(f"{stem} [{f.key}]{ext}", share.colon)
        used[f.local.lower()] = f.key
        placed.add(f.key)
        manifest[f.key] = f.local
        changed = True

    if changed and not dry_run:
        save_manifest(fs, output_dir, share.manifest_name, manifest)


def select_only(files: list[RemoteFile], patterns: tuple[str, ...]) -> list[RemoteFile]:
    """Case-insensitive globs on the share path. ``*`` also crosses folders."""
    lowered = [p.lower() for p in patterns]
    return [f for f in files if any(fnmatch.fnmatch(f.path.lower(), p) for p in lowered)]


def folders_for(files: list[RemoteFile], folders: list[RemoteFolder]) -> list[RemoteFolder]:
    """Only the folders that are ancestors of the given files."""
    needed: set[str] = set()
    for f in files:
        parent = f.local.rpartition("/")[0]
        while parent and parent not in needed:
            needed.add(parent)
            parent = parent.rpartition("/")[0]
    return [f for f in folders if f.local in needed]


def resolve_case_twins(files: list[RemoteFile], fs, output_dir: str, key=str.lower) -> None:
    """Give remote files that collide on a case-insensitive name their own local names.

    A server can hold ``Foo.pdf`` and ``FOO.pdf`` as different files, but a
    case-insensitive share can't. Left alone, each would look like a size mismatch
    against the other, and the smaller could overwrite the larger.

    Each twin is matched by size to a file already there: its own name, the same name
    in other casing, or a ``name (N).ext`` copy. A twin with no match gets a free
    ``name (N).ext``, so it never overwrites anything. Listing order decides who gets
    what, which keeps the result stable between runs.
    """
    groups: dict[str, list[RemoteFile]] = {}
    for f in files:
        groups.setdefault("/".join(key(part) for part in f.local.split("/")), []).append(f)

    for group in groups.values():
        if len(group) < 2:
            continue
        parent = group[0].local.rpartition("/")[0]
        listing = fs.list_files(_dest(fs, output_dir, parent))
        sizes = {name: st.size for name, st in listing.items()}
        claimed: set[str] = set()
        pending = []

        for f in group:
            name = f.local.rpartition("/")[2]
            if f.size is not None and sizes.get(name) == f.size:
                claimed.add(name)
            else:
                pending.append(f)

        unmatched = []
        for f in pending:
            name = f.local.rpartition("/")[2]
            stem, dot, ext = name.rpartition(".")
            base = stem if dot else name
            suffix = f".{ext}" if dot else ""
            wanted = key(name)
            match = next(
                (n for n, size in sizes.items() if n not in claimed and size == f.size and _twin_key(n, key) == wanted),
                None,
            )
            if match is None:
                unmatched.append((f, name, base, suffix))
                continue
            claimed.add(match)
            f.local = _join_rel(parent, match)

        taken = {key(n) for n in sizes} | {key(n) for n in claimed}
        for f, name, base, suffix in unmatched:
            candidate, n = name, 0
            while key(candidate) in taken:
                n += 1
                candidate = f"{base} ({n}){suffix}"
            taken.add(key(candidate))
            f.local = _join_rel(parent, candidate)
        print(f"  Note: {len(group)} remote files differ only by case in {parent or '(top folder)'}; kept separately.")


_COPY_NUMBER = re.compile(r" \(\d+\)(?=\.[^.]*$|$)")


def _twin_key(name: str, key) -> str:
    """The key a name has once a `` (N)`` copy number is taken off, so ``a (2).pdf``
    counts as a copy of ``a.pdf``."""
    return key(_COPY_NUMBER.sub("", name, count=1)) if _COPY_NUMBER.search(name) else key(name)


def warn_collisions(files: list[RemoteFile]) -> None:
    """Two remote names mapping to one local name would otherwise re-download each
    other on every run with no visible cause."""
    seen: dict[str, str] = {}
    for f in files:
        clash = seen.setdefault(f.local, f.path)
        if clash != f.path:
            print(f"  Warning: {clash!r} and {f.path!r} both map to {f.local!r}")


class Existing:
    """What's at the destination, listed lazily once per directory.

    Lookups fall back to a case-insensitive match. A case-sensitive server then
    treats a remote case-only rename as a skip, like a Windows drive would, and
    reconcile_case renames the file afterwards. A scan that already listed the
    tree seeds it, so nothing is listed twice.
    """

    def __init__(self, fs, output_dir: str, scan: "LocalScan | None" = None):
        self.fs, self.output_dir = fs, output_dir
        self._dirs: dict[str, tuple[dict, dict]] = {}
        if scan is not None:
            for rel_dir, files in scan.stats.items():
                self._dirs[rel_dir] = (files, {n.lower(): n for n in files})

    def get(self, rel: str):
        """``(actual path, FileStat)`` for a relative path, or ``(None, None)``."""
        parent, _, name = rel.rpartition("/")
        listing = self._dirs.get(parent)
        if listing is None:
            listing = self._list(parent)
            self._dirs[parent] = listing
        exact, by_lower = listing
        actual = name if name in exact else by_lower.get(name.lower())
        if actual is None:
            return None, None
        return _dest(self.fs, self.output_dir, _join_rel(parent, actual)), exact[actual]

    def _list(self, parent: str):
        path = _dest(self.fs, self.output_dir, parent)
        try:
            files = self.fs.list_files(path)
        except RETRYABLE_ERRORS:
            files = self.fs.list_files(path)  # SFTPFS reconnects; one retry
        return files, {n.lower(): n for n in files}


# -- manifest ---------------------------------------------------------------


def load_manifest(fs, output_dir: str, name: str | None) -> dict:
    """Read a manifest. ``{}`` only if it's missing or isn't valid JSON."""
    if not name:
        return {}
    path = fs.join(output_dir, name)
    for attempt in range(2):
        try:
            with fs.open(path, "rb") as fp:
                data = json.loads(fp.read().decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except ValueError as e:
            print(f"  Warning: ignoring unreadable manifest {name}: {e}")
            return {}
        except RETRYABLE_ERRORS as e:
            if attempt:
                raise ManifestError(f"Could not read {fs.describe(path)}: {e}") from e
    return {}


def save_manifest(fs, output_dir: str, name: str | None, manifest: dict) -> None:
    if not name or not manifest:
        return
    try:
        fs.makedirs(output_dir)
        path = fs.join(output_dir, name)
        tmp = path + ".tmp"
        data = json.dumps(manifest, ensure_ascii=False, indent=0, sort_keys=True)
        with fs.open(tmp, "wb") as fp:
            fp.write(data.encode("utf-8"))
        fs.replace(tmp, path)
    except RETRYABLE_ERRORS as e:
        print(f"  Warning: could not write manifest: {e}")


# -- dates ------------------------------------------------------------------


def _set_time(fs, path: str, timestamp: float, result: SyncResult) -> bool:
    """A refused date doesn't stop the run, but it's counted, never reported as done."""
    try:
        fs.set_mtime(path, timestamp)
        return True
    except RETRYABLE_ERRORS as e:
        print(f"  Warning: could not set date on {path}: {e}")
        result.date_failures += 1
        return False


def sync_time(fs, path: str, current: float | None, wanted: float | None, dry_run: bool, result: SyncResult) -> bool:
    """Set the date only if it differs. True if it changed or, in a dry run, would."""
    if not wanted or (current is not None and abs(current - wanted) < 1):
        return False
    return dry_run or _set_time(fs, path, wanted, result)


def _folder_stats(folders: list[RemoteFolder], fs, output_dir: str, scan: "LocalScan | None" = None) -> dict:
    """A folder with no date to set needs only to be known to exist, which a scan
    already says. Anything else is stat'ed for its date."""
    stats = {}
    for f in folders:
        if scan is not None and not f.mtime:
            stats[f.local] = FileStat(0, 0.0, True) if f.local in scan.dirs else None
        else:
            stats[f.local] = fs.stat(_dest(fs, output_dir, f.local))
    return stats


def _create_folders(folders: list[RemoteFolder], fs, output_dir: str, dry_run: bool, scan=None) -> dict:
    stats = _folder_stats(folders, fs, output_dir, scan)
    for folder in folders:
        if stats[folder.local] is None:
            if dry_run:
                print(f"  DRY RUN: would create directory {folder.local}")
            else:
                fs.makedirs(_dest(fs, output_dir, folder.local))
                print(f"  Created directory: {folder.local}")
    return stats


def _set_folder_dates(
    folders: list[RemoteFolder], fs, output_dir: str, stats: dict, touched: set[str], dry_run: bool, result: SyncResult
) -> int:
    """Deepest first, so dating a child can't disturb its parent.

    Folders a download landed in are always re-dated, because the write bumped them.
    """
    updated = 0
    for folder in sorted(folders, key=lambda f: f.local.count("/"), reverse=True):
        if not folder.mtime:
            continue
        st = stats.get(folder.local)
        if st is None and dry_run:
            continue
        if st is not None and folder.local not in touched and abs(st.mtime - folder.mtime) < 1:
            continue
        if dry_run:
            print(f"  DRY RUN: would set directory date: {folder.local}")
        elif _set_time(fs, _dest(fs, output_dir, folder.local), folder.mtime, result):
            print(f"  Directory date set: {folder.local}")
        else:
            continue
        updated += 1
    return updated


# -- transfer ---------------------------------------------------------------


class ContentHash:
    """A content hash a server publishes, computed the way that server writes it."""

    def __init__(self, new, encode, case_sensitive: bool):
        self.new, self.encode, self.case_sensitive = new, encode, case_sensitive

    def digest(self, hasher) -> str:
        return self.encode(hasher.digest())

    def same(self, ours: str, theirs: str) -> bool:
        return ours == theirs if self.case_sensitive else ours.lower() == theirs.lower()


# A Share whose verify_kind is one of these publishes that hash in RemoteFile.etag,
# so --verify reads files back and hashes downloads as they stream.
CONTENT_HASHES = {
    "md5": ContentHash(hashlib.md5, bytes.hex, case_sensitive=False),
    "sha256": ContentHash(hashlib.sha256, bytes.hex, case_sensitive=False),
    "blake2b-240": ContentHash(
        lambda: hashlib.blake2b(digest_size=30), lambda d: base64.b64encode(d).decode("ascii"), case_sensitive=True
    ),
}


def file_hash(fs, path: str, kind: str) -> str:
    """Hash a file at the destination. Over SFTP this reads the whole file back."""
    content_hash = CONTENT_HASHES[kind]
    hasher = content_hash.new()
    with fs.open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            hasher.update(chunk)
    return content_hash.digest(hasher)


def download_file(share: Share, file: RemoteFile, fs, local_path: str, expected_hash: str | None = None) -> int:
    """Stream to ``<file>.part``, then rename once the size is right.

    A failed attempt keeps the partial file so the retry, or a later run, can
    resume. A provider that can't resume starts over.

    With ``expected_hash``, a download that started from the first byte is hashed as it
    streams with the share's content hash, so nothing has to be read back. A mismatch discards it, leaving any older
    copy in place, and isn't retried because the next attempt would likely match no better.
    """
    part = local_path + PART_SUFFIX
    expected = file.size
    last_error: Exception | None = None

    for attempt in range(TRANSFER_ATTEMPTS):
        progress = Progress()
        try:
            st = fs.stat(part)
            have = st.size if st else 0
            if have and not share.can_resume:
                have = 0
            if expected is not None and have > expected:
                fs.remove(part)
                have = 0
            if have and have == expected:
                fs.replace(part, local_path)  # finished earlier; only the rename was lost
                return have

            transfer = share.open_transfer(file, have)
            if expected is None:
                expected = file.size  # some providers only learn it when the transfer starts
            wrote = transfer.offset
            mode = "ab" if transfer.offset else "wb"
            content_hash = CONTENT_HASHES[share.verify_kind] if expected_hash else None
            hasher = content_hash.new() if content_hash and not transfer.offset else None
            with fs.open(part, mode) as fp:
                for chunk in transfer.chunks:
                    fp.write(chunk)
                    if hasher:
                        hasher.update(chunk)
                    wrote += len(chunk)
                    if expected:
                        progress.update(
                            f"  Downloading... {wrote * 100 // expected}% "
                            f"({fmt_size(wrote)}/{fmt_size(expected)})"
                        )
            progress.clear()
            if expected is not None and wrote != expected:
                raise IOError(f"Size mismatch: got {wrote}, expected {expected}")
            if hasher and not content_hash.same(content_hash.digest(hasher), expected_hash):
                fs.remove(part)
                raise PermanentTransferError(f"the download doesn't match the server's {share.verify_kind} hash")
            fs.replace(part, local_path)
            return wrote
        except CorruptDownload:
            progress.clear()
            try:
                fs.remove(part)
            except (FileNotFoundError, *RETRYABLE_ERRORS):
                pass
            raise
        except (PermanentTransferError, AbortRun):
            progress.clear()
            raise
        except StalePartial as e:
            progress.clear()
            fs.remove(part)
            last_error = e
        except RETRYABLE_ERRORS as e:
            progress.clear()
            last_error = e

        if attempt < TRANSFER_ATTEMPTS - 1:
            wait = 5 * (attempt + 1)
            print(f"  Retry {attempt + 1}/{TRANSFER_ATTEMPTS - 1} in {wait}s ({last_error.__class__.__name__})...")
            time.sleep(wait)
    raise last_error


def download_files(
    files: list[RemoteFile],
    folders: list[RemoteFolder],
    fs,
    output_dir: str,
    share: Share,
    options: SyncOptions,
    result: SyncResult,
    scan: "LocalScan | None" = None,
) -> None:
    dry_run = options.dry_run
    folder_stats = _create_folders(folders, fs, output_dir, dry_run, scan)
    warn_collisions(files)

    manifest = load_manifest(fs, output_dir, share.manifest_name) if share.verify_kind == "etag" else {}
    hashed = share.verify_kind in CONTENT_HASHES
    content_hash = CONTENT_HASHES.get(share.verify_kind)

    def same_tag(recorded: str, tag: str) -> bool:
        return content_hash.same(recorded, tag) if content_hash else recorded == tag

    existing = Existing(fs, output_dir, scan)
    made_dirs: set[str] = set()
    touched: set[str] = set()
    dirty = False
    last_save = time.monotonic()
    date_word = "would update" if dry_run else "updated"
    total = len(files)

    try:
        for i, f in enumerate(files, 1):
            prefix = f"[{i}/{total}] {f.local}"
            found, st = existing.get(f.local)
            note = ""

            if st is not None:
                if f.size is not None and st.size == f.size:
                    recorded = manifest.get(f.local) if share.verify_kind == "etag" else None
                    if options.verify and f.etag and hashed:
                        try:
                            recorded = file_hash(fs, found, share.verify_kind)
                        except RETRYABLE_ERRORS as e:
                            print(f"{prefix} — ERROR: could not read it back to verify: {e}")
                            result.errors += 1
                            continue
                    changed = options.verify and recorded and f.etag and not same_tag(recorded, f.etag)
                    if not changed:
                        if options.verify and recorded and f.etag:
                            reason = "verified"
                        elif options.verify:
                            reason = "size match, nothing recorded to verify against"
                        else:
                            reason = "exists"
                        msg = f"{prefix} — SKIP ({reason}, {fmt_size(f.size)})"
                        result.skipped += 1
                        if sync_time(fs, found, st.mtime, f.mtime, dry_run, result):
                            result.dates_changed += 1
                            msg += f" [date {date_word}]"
                        print(msg)
                        continue
                    note = " — RE-DOWNLOAD (changed on server)"
                    result.reverified += 1
                elif f.size is not None:
                    note = f" — RE-DOWNLOAD (size mismatch: local={st.size}, remote={f.size})"
                elif st.size > 0:
                    msg = f"{prefix} — SKIP (exists, {fmt_size(st.size)}, no remote size)"
                    result.skipped += 1
                    if sync_time(fs, found, st.mtime, f.mtime, dry_run, result):
                        result.dates_changed += 1
                        msg += f" [date {date_word}]"
                    print(msg)
                    continue

            if dry_run:
                print(f"{prefix}{note} — DRY RUN (would download, {fmt_size(f.size)})")
                result.would_download += 1
                continue

            print(f"{prefix}{note}")
            parent = f.local.rpartition("/")[0]
            local_path = _dest(fs, output_dir, f.local)
            try:
                if parent not in made_dirs:
                    fs.makedirs(_dest(fs, output_dir, parent))
                    made_dirs.add(parent)
                expected_hash = f.etag if options.verify and hashed else None
                wrote = download_file(share, f, fs, local_path, expected_hash)
                result.downloaded += 1
                touched.add(parent)
                if f.etag and share.verify_kind == "etag" and share.manifest_name:
                    manifest[f.local] = f.etag
                    dirty = True
                    if time.monotonic() - last_save >= MANIFEST_SAVE_INTERVAL:
                        save_manifest(fs, output_dir, share.manifest_name, manifest)
                        dirty = False
                        last_save = time.monotonic()
                msg = f"  -> OK ({fmt_size(wrote)})"
                if f.mtime and _set_time(fs, local_path, f.mtime, result):
                    result.dates_changed += 1
                    msg += " [date set]"
                print(msg)
            except AbortRun:
                result.errors += 1
                raise
            except (PermanentTransferError, *RETRYABLE_ERRORS) as e:
                print(f"  -> ERROR: {e}")
                result.errors += 1
    finally:
        # Also on Ctrl-C or an abort: keep what has been recorded so far.
        if dirty:
            save_manifest(fs, output_dir, share.manifest_name, manifest)

    result.dates_changed += _set_folder_dates(folders, fs, output_dir, folder_stats, touched, dry_run, result)

    print()
    print(
        f"Done: {result.downloaded} downloaded, {result.skipped} skipped, "
        f"{result.dates_changed} dates {date_word}, {result.errors} errors"
    )
    if dry_run:
        print(f"Would download: {result.would_download}")
    if result.reverified:
        print(f"Changed on server: {result.reverified}")


def update_dates_only(files, folders, fs, output_dir: str, options: SyncOptions, result: SyncResult, scan=None) -> None:
    existing = Existing(fs, output_dir, scan)
    missing = no_date = 0
    word = "would update" if options.dry_run else "updated"
    for f in files:
        found, st = existing.get(f.local)
        if st is None:
            missing += 1
        elif not f.mtime:
            no_date += 1
        elif sync_time(fs, found, st.mtime, f.mtime, options.dry_run, result):
            result.dates_changed += 1
            print(f"  Date {word}: {f.local}")
    stats = _folder_stats(folders, fs, output_dir)
    result.dates_changed += _set_folder_dates(folders, fs, output_dir, stats, set(), options.dry_run, result)
    print(f"Dates: {result.dates_changed} {word}, {missing} files not found locally, {no_date} had no date info")


def seed_manifest(files, fs, output_dir: str, share: Share, options: SyncOptions) -> None:
    """Record current server tags for files already present with the right size,
    so ``--verify`` can detect later changes. Never touches file contents."""
    if share.verify_kind != "etag":
        raise ShareError(f"--seed-manifest isn't available for {share.provider_name}")
    manifest = load_manifest(fs, output_dir, share.manifest_name)
    existing = Existing(fs, output_dir)
    seeded = already = mismatch = missing = no_tag = 0
    for f in files:
        _, st = existing.get(f.local)
        if st is None:
            missing += 1
        elif not f.etag:
            no_tag += 1
        elif f.size is not None and st.size != f.size:
            mismatch += 1
        elif manifest.get(f.local) == f.etag:
            already += 1
        else:
            if options.dry_run:
                print(f"  would seed: {f.local}")
            else:
                manifest[f.local] = f.etag
            seeded += 1
    if seeded and not options.dry_run:
        save_manifest(fs, output_dir, share.manifest_name, manifest)
    word = "would seed" if options.dry_run else "seeded"
    print(
        f"Manifest: {seeded} {word}, {already} already current, {mismatch} size mismatch, "
        f"{missing} not found locally, {no_tag} had no server tag"
    )


# -- rollover ---------------------------------------------------------------


def rollover_dirs(folders: list[RemoteFolder], fs, output_dir: str, archive: str, share: Share, dry_run: bool) -> list[str]:
    """Follow a server moving old top-level folders into an archive folder.

    Without this, the next sync downloads the moved folder again under the archive
    and reports the old copy as orphans. A folder whose archived copy already exists
    locally is left alone, since merging could overwrite good files.
    """
    archive = safe_path(archive.strip("/"), share.colon)
    server_top, server_archived = set(), set()
    for f in folders:
        if "/" not in f.local:
            server_top.add(f.local)
        elif f.local.rsplit("/", 1)[0] == archive:
            server_archived.add(f.local.rsplit("/", 1)[1])

    if not fs.isdir(output_dir):
        return []
    _root, local_top, _files = next(iter(fs.walk(output_dir)))
    candidates = sorted(d for d in local_top if d != archive and d not in server_top and d in server_archived)
    if not candidates:
        return []

    archive_dir = _dest(fs, output_dir, archive)
    local_archived = set(next(iter(fs.walk(archive_dir)))[1]) if fs.isdir(archive_dir) else set()
    # Load first, so an unreadable manifest stops the run before the tree changes.
    manifest = load_manifest(fs, output_dir, share.manifest_name)

    moved = []
    for name in candidates:
        target = f"{archive}/{name}"
        if name in local_archived:
            print(f"  WARNING: {name} moved to {target} on the server, but both exist locally; leaving it.")
            continue
        if dry_run:
            print(f"  DRY RUN: would move {name} -> {target}")
        else:
            print(f"  Moving {name} -> {target}")
            fs.makedirs(archive_dir)
            fs.replace(_dest(fs, output_dir, name), _dest(fs, output_dir, target))
            prefix = name + "/"
            for key in [k for k in manifest if k.startswith(prefix)]:
                manifest[target + "/" + key[len(prefix) :]] = manifest.pop(key)
        moved.append(name)
    if moved and not dry_run:
        save_manifest(fs, output_dir, share.manifest_name, manifest)
    return moved


# -- local scan, case and orphans -------------------------------------------


@dataclass
class LocalScan:
    """One walk of the destination, shared by case reconciling and orphan handling.

    ``stats`` has every regular file's FileStat by directory, when the filesystem
    can give it during the walk.
    """

    files: set[str] = field(default_factory=set)
    dirs: dict[str, tuple[list[str], list[str]]] = field(default_factory=dict)
    stats: dict[str, dict[str, FileStat]] = field(default_factory=dict)


def _walk_stats(fs, top: str):
    walker = getattr(fs, "walk_stats", None)
    if walker is not None:
        yield from walker(top)
    else:
        for dirpath, dirnames, filenames in fs.walk(top):
            yield dirpath, dirnames, {name: None for name in filenames}


def scan_local(fs, output_dir: str, scope: str, share: Share, keep_markers: tuple[str, ...]) -> LocalScan:
    """Walk ``scope`` under the output folder.

    Anything whose name contains a keep-marker is left out, along with its subtree:
    that's content deliberately kept after it left the server. OS junk, partial
    files and our own state files are left out too.
    """
    scan = LocalScan()
    markers = tuple(m for m in keep_markers if m)
    top = _dest(fs, output_dir, scope)
    if not fs.isdir(top):
        return scan
    ignored = set(IGNORED_NAMES)
    if share.manifest_name:
        ignored |= {share.manifest_name.lower(), share.manifest_name.lower() + ".tmp"}

    for dirpath, dirnames, file_stats in _walk_stats(fs, top):
        rel_dir = _rel_to(dirpath, output_dir)
        filenames = list(file_stats)
        scan.dirs[rel_dir] = (list(dirnames), filenames)
        if all(st is not None for st in file_stats.values()):
            scan.stats[rel_dir] = dict(file_stats)
        if markers:
            dirnames[:] = [d for d in dirnames if not any(m in d for m in markers)]
        for name in filenames:
            if name.lower() in ignored or name.endswith(PART_SUFFIX) or (share.junk_prefixes and name.startswith(share.junk_prefixes)):
                continue
            if markers and any(m in name for m in markers):
                continue
            scan.files.add(_join_rel(rel_dir, name))
    return scan


_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_ESCAPED_CHARS = set('\\:*?"<>|') | {chr(c) for c in range(0x20)}


def _unescape(match: re.Match) -> str:
    char = chr(int(match.group(1), 16))
    return char if char in _ESCAPED_CHARS else match.group(0)


def loose_key(name: str) -> str:
    """What two names share when ``Share.loose_names`` counts them as one: case,
    Unicode normalization, %xx escapes of reserved characters, leading spaces and
    trailing spaces and dots don't count. A literal ``%20`` in a name stays."""
    name = unicodedata.normalize("NFC", _ESCAPE.sub(_unescape, name)).casefold()
    return name.lstrip(" ").rstrip(". ")


MAX_LOOSE_DIRS = 8  # folders one remote folder may match, so a pathological tree can't blow up


def adopt_existing_names(listing: Listing, scan: LocalScan) -> int:
    """Point each remote folder and file at what's already on disk under a loosely
    equal name, so it's skipped, dated and kept out of the orphans under that name.

    A remote folder can match several local ones, such as ``Name`` and ``Name ``,
    when tools wrote it two ways and split its files between them. Each file is
    looked for in all of them: its exact name first, then a single loose match. The
    best match (the exact name, else the first found) is where the folder itself, and
    any file not found, goes. Remote files sharing a loose name in one folder are left
    for resolve_case_twins to match by size. Returns how many entries changed.
    """
    subdir_index: dict[str, dict[str, list[str]]] = {}
    file_index: dict[str, dict[str, list[str]]] = {}
    resolved: dict[str, list[str]] = {"": [""]}

    def children(parent: str, name: str) -> list[str]:
        entries = scan.dirs.get(parent)
        if entries is None:
            return [_join_rel(parent, name)]  # new, or outside the scanned scope: as it is
        index = subdir_index.get(parent)
        if index is None:
            index = subdir_index[parent] = {}
            for d in entries[0]:
                index.setdefault(loose_key(d), []).append(d)
        found = index.get(loose_key(name), [])
        ordered = ([name] if name in entries[0] else []) + [d for d in found if d != name]
        return [_join_rel(parent, d) for d in ordered]

    def dirs_for(rel: str) -> list[str]:
        if rel in resolved:
            return resolved[rel]
        parent, _, name = rel.rpartition("/")
        out: list[str] = []
        for candidate in dirs_for(parent):
            for child in children(candidate, name):
                if child not in out and len(out) < MAX_LOOSE_DIRS:
                    out.append(child)
        resolved[rel] = out or [_join_rel(dirs_for(parent)[0], name)]
        return resolved[rel]

    def loose_files(rel_dir: str) -> dict[str, list[str]]:
        index = file_index.get(rel_dir)
        if index is None:
            index = file_index[rel_dir] = {}
            for n in scan.dirs.get(rel_dir, ((), ()))[1]:
                index.setdefault(loose_key(n), []).append(n)
        return index

    changed = 0
    for folder in listing.folders:
        actual = dirs_for(folder.local)[0]
        changed += actual != folder.local
        folder.local = actual

    groups: dict[tuple[str, str], list[tuple[RemoteFile, list[str], str]]] = {}
    for f in listing.files:
        parent, _, name = f.local.rpartition("/")
        candidates = dirs_for(parent)
        groups.setdefault((candidates[0], loose_key(name)), []).append((f, candidates, name))

    for (primary, key), group in groups.items():
        home = primary
        if len(group) > 1:
            for d in group[0][1]:
                if any(_twin_key(n, loose_key) == key for n in scan.dirs.get(d, ((), ()))[1]):
                    home = d
                    break
        for f, candidates, name in group:
            target = None
            if len(group) == 1:
                for d in candidates:
                    entries = scan.dirs.get(d)
                    if entries is not None and name in entries[1]:
                        target = _join_rel(d, name)
                        break
                if target is None:
                    for d in candidates:
                        found = loose_files(d).get(key, [])
                        if len(found) == 1:
                            target = _join_rel(d, found[0])
                        if found:
                            break
            target = target or _join_rel(home, name)
            changed += target != f.local
            f.local = target
    return changed


def reconcile_case(files: list[RemoteFile], fs, output_dir: str, local_files: set[str], dry_run: bool) -> int:
    """Rename local files that differ from a remote file only by case.

    Otherwise a remote case-only rename looks like a skip plus a phantom orphan.
    If both casings exist, which only a case-sensitive destination allows, nothing
    happens, because a rename would overwrite the good copy.
    """
    exact = {f.local for f in files}
    by_lower = {p.lower(): p for p in exact}
    renamed = 0
    for rel in sorted(local_files):
        if rel in exact:
            continue
        canonical = by_lower.get(rel.lower())
        if canonical is None or canonical in local_files:
            continue
        if dry_run:
            print(f"  DRY RUN: would rename {rel} -> {canonical}")
            renamed += 1
            continue
        try:
            fs.makedirs(_dest(fs, output_dir, canonical.rpartition("/")[0]))
            fs.replace(_dest(fs, output_dir, rel), _dest(fs, output_dir, canonical))
            local_files.discard(rel)
            local_files.add(canonical)
            print(f"  Renamed {rel} -> {canonical}")
            renamed += 1
        except RETRYABLE_ERRORS as e:
            print(f"  FAILED to rename {rel}: {e}")
    return renamed


@dataclass
class OrphanClaim:
    """What one share claims in its output folder. Sites that sync into the same
    folder are judged together, so each one's files aren't the other's orphans."""

    fs: object
    output_dir: str
    scan: LocalScan
    files: set[str]
    folders: set[str]
    has_files: bool
    partial: bool  # --only or --path


def find_orphans(claimed: set[str], local_files: set[str]) -> list[str]:
    return sorted(p for p in local_files if p not in claimed)


def refuse_delete_reason(claims: list[OrphanClaim], options: SyncOptions, orphans: list[str], local_total: int) -> str | None:
    """Why deleting these orphans isn't safe, or None.

    Remote folders do get emptied or cut down, which makes a local copy look like
    orphans when it may be the only copy left.
    """
    if not any(c.has_files for c in claims):
        return "the remote listing has no files, so every local file looks like an orphan"
    if any(c.partial for c in claims):
        return "this run covers only part of the share (--only or --path)"
    if local_total and options.delete_max_pct < 100:
        pct = len(orphans) * 100 / local_total
        if pct > options.delete_max_pct:
            return (
                f"it would remove {pct:.0f}% of the local files, over the "
                f"{options.delete_max_pct:g}% limit (raise it with --delete-max-pct)"
            )
    return None


def delete_orphans(orphans, fs, output_dir: str, keep: set[str], scan: LocalScan, dry_run: bool) -> tuple[int, int, list[str]]:
    """Delete orphan files, then remove folders that left empty.

    Returns ``(deleted, errors, removed folders)``.
    """
    deleted = errors = 0
    gone: set[str] = set()
    for rel in orphans:
        if dry_run:
            print(f"  would delete: {rel}")
            deleted += 1
            continue
        try:
            fs.remove(_dest(fs, output_dir, rel))
            print(f"  DELETED: {rel}")
            deleted += 1
            gone.add(rel)
            scan.files.discard(rel)
        except RETRYABLE_ERRORS as e:
            print(f"  FAILED to delete {rel}: {e}")
            errors += 1

    removed: set[str] = set()
    # Computed from the scan, not a second walk. rmdir refuses a non-empty folder,
    # so a stale listing can't lose data. A folder holding only ignored files stays.
    for rel in sorted(scan.dirs, key=lambda d: d.count("/"), reverse=True):
        if not rel or rel in keep:
            continue
        subdirs, names = scan.dirs[rel]
        if any(_join_rel(rel, n) not in gone for n in names):
            continue
        if any(_join_rel(rel, d) not in removed for d in subdirs):
            continue
        if dry_run:
            print(f"  would remove empty dir: {rel}")
        else:
            try:
                fs.rmdir(_dest(fs, output_dir, rel))
            except RETRYABLE_ERRORS:
                continue
            print(f"  removed empty dir: {rel}")
        removed.add(rel)
    return deleted, errors, sorted(removed)


def set_root_date(fs, path: str, mtime: float | None, dry_run: bool, result: SyncResult) -> bool:
    """Date a top folder. Runs last, because every write into it bumps its date."""
    st = fs.stat(path)
    if st is None or not sync_time(fs, path, st.mtime, mtime, dry_run, result):
        return False
    print(f"  {'DRY RUN: would set' if dry_run else 'Set'} directory date: (output root)")
    return True


# -- the whole run ----------------------------------------------------------


def sync_listing(listing: Listing, share: Share, fs, output_dir: str, options: SyncOptions) -> SyncResult:
    """Everything after the listing, for one share."""
    result = SyncResult()
    assign_local_paths(listing, share, options)
    if share.stable_names:
        assign_stable_names(listing.files, share, fs, output_dir, options.dry_run)
    scan = None
    if share.loose_names:
        # One walk, before anything changes, serves name matching, the skip checks
        # and the orphan check.
        scope = "" if options.reroot else safe_path(options.subpath.strip("/"), share.colon)
        scan = scan_local(fs, output_dir, scope, share, options.keep_markers)
        adopted = adopt_existing_names(listing, scan)
        if adopted:
            print(f"  Matched {adopted} name(s) to how they're already stored.")
    resolve_case_twins(listing.files, fs, output_dir, loose_key if share.loose_names else str.lower)

    selected, folders = listing.files, listing.folders
    if options.only:
        selected = select_only(listing.files, options.only)
        print(f"  Selected by --only: {len(selected)}")
        if not selected:
            raise ShareError("No files match --only.")
        folders = folders_for(selected, listing.folders)

    if options.rollover:
        print(f"\nChecking for folders moved into {options.rollover!r} on the server...")
        moved = rollover_dirs(listing.folders, fs, output_dir, options.rollover, share, options.dry_run)
        if moved:
            scan = None  # the tree changed under the scan
        if not moved:
            print("  None.")
        elif options.dry_run:
            print("  (dry run: the preview below still shows those folders as new downloads)")

    if options.dates_only:
        print("\nUpdating dates only...")
        update_dates_only(selected, folders, fs, output_dir, options, result, scan)
    elif options.seed_manifest:
        print("\nSeeding the manifest from existing files...")
        seed_manifest(selected, fs, output_dir, share, options)
    else:
        print()
        download_files(selected, folders, fs, output_dir, share, options, result, scan)
        _finish_local_tree(listing, share, fs, output_dir, options, result, scan)

    # In a whole-share run the output folder is the share root. For a subfolder, the
    # date belongs to that subfolder, wherever it landed.
    scope = "" if options.reroot else safe_path(options.subpath.strip("/"), share.colon)
    if set_root_date(fs, _dest(fs, output_dir, scope), listing.root_mtime, options.dry_run, result):
        result.dates_changed += 1
    if result.date_failures:
        print(f"\n{result.date_failures} date(s) could not be set; see the warnings above.")
        result.errors += result.date_failures
    return result


def _finish_local_tree(
    listing: Listing, share: Share, fs, output_dir: str, options: SyncOptions, result: SyncResult, scan=None
) -> None:
    """Case reconciling and orphans, judged against the whole listing.

    ``--only`` narrows what's fetched, but not what counts as being on the server.
    A scan from before the downloads still holds: files fetched since are remote
    files, so they can't be orphans.
    """
    if scan is None:
        scope = "" if options.reroot else safe_path(options.subpath.strip("/"), share.colon)
        scan = scan_local(fs, output_dir, scope, share, options.keep_markers)

    print("\nReconciling filename casing with the server...")
    result.renamed = reconcile_case(listing.files, fs, output_dir, scan.files, options.dry_run)
    print(f"Reconciled {result.renamed} file(s)." if result.renamed else "No casing differences found.")

    claim = OrphanClaim(
        fs,
        output_dir,
        scan,
        {f.local for f in listing.files},
        {f.local for f in listing.folders},
        bool(listing.files),
        bool(options.only or options.subpath),
    )
    if options.defer_orphans:
        result.claim = claim
        print("\nThe orphan check runs at the end, together with any other site sharing this folder.")
        return
    judge_orphans([claim], options, result)


def judge_orphans(claims: list[OrphanClaim], options: SyncOptions, result: SyncResult) -> None:
    """Report, and with --delete-orphans delete, local files no claim includes.

    For several sites sharing a folder, the last scan is used: files an earlier site
    fetched since are claimed, so they can't be orphans, and the refusals apply to
    the sites together.
    """
    if options.no_orphans and not options.delete_orphans:
        print("\nSkipping the orphan check (--no-orphans).")
        return
    last = claims[-1]
    fs, output_dir, scan = last.fs, last.output_dir, last.scan

    print("\nChecking for orphaned local files...")
    if options.keep_markers:
        print(f"  (excluding names marked: {', '.join(repr(m) for m in options.keep_markers)})")
    claimed = set().union(*(c.files for c in claims))
    result.orphans = find_orphans(claimed, scan.files)
    if not result.orphans:
        print("No orphaned files found.")
        return
    print(f"Found {len(result.orphans)} local file(s) not present on the server:")
    for orphan in result.orphans:
        print(f"  ORPHAN: {orphan}")

    if not options.delete_orphans:
        print("\nThey have NOT been deleted. Use --delete-orphans to remove them (add --dry-run to preview).")
        return

    reason = refuse_delete_reason(claims, options, result.orphans, len(scan.files))
    if reason:
        print(f"\nRefusing to delete orphans: {reason}.")
        result.errors += 1
        return
    print(f"\n{'Would delete' if options.dry_run else 'Deleting'} orphan files...")
    keep = set().union(*(c.folders for c in claims))
    deleted, errs, removed = delete_orphans(result.orphans, fs, output_dir, keep, scan, options.dry_run)
    result.deleted, result.errors = deleted, result.errors + errs
    print(f"Orphans: {deleted} {'would delete' if options.dry_run else 'deleted'}, {len(removed)} empty folder(s), {errs} errors")
