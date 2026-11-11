"""trove for other programs.

Scrapers and other tools that find links should call trove rather than carry their
own download code. Two ways, with the same behaviour:

* The command line: ``trove get --from-file - --json -`` reads links on stdin and
  prints a JSON report (see trove.report) on stdout, with progress on stderr. The
  exit codes are stable; see docs/helpers.md.
* This module, in the same process::

      from trove import api

      report = api.get(["https://share.example/s/TOKEN"], output="nas:/files/{provider}")
      for share in report.shares:
          print(share.status, share.link, share.downloaded)

Both use the user's config file for destinations and credentials, sync every link
over one connection per destination, and stop the batch when a site is down or a
quota runs out (``report.aborted``), marking the links not reached as ``not_run``.

What's here is the stable surface: these functions, their keyword arguments, and
the report fields, and ``ConfigError``. Everything else in the package may change.
"""

from __future__ import annotations

import contextlib
import io
import os
from dataclasses import dataclass
from typing import Iterable, Iterator

from trove.config import load_config
from trove.errors import ConfigError
from trove.report import RunReport, ShareReport

__all__ = [
    "get", "sync", "recognizes", "identify", "sites", "open_folder", "config_path",
    "ConfigError", "RunReport", "ShareReport", "LinkInfo", "SiteInfo", "Folder", "FileInfo",
]


def recognizes(url: str) -> bool:
    """True if a provider can sync this link as it stands. A shortener isn't
    recognized, since only following it tells where it leads; hand those to ``get``
    anyway, which follows them."""
    from trove.providers import is_provider_url

    return is_provider_url(url)


def _run(argv: list[str], config: str | None, quiet: bool, runner) -> RunReport:
    from trove.cli import build_parser

    args = build_parser().parse_args((["--config", config] if config else []) + argv)
    loaded = load_config(args.config)
    if not quiet:
        return runner(args, loaded)
    with contextlib.redirect_stdout(io.StringIO()):
        return runner(args, loaded)


def get(
    links: Iterable[str],
    *,
    output: str | None = None,
    dest: str | None = None,
    name: str | None = None,
    path: str | None = None,
    password: str | None = None,
    only: Iterable[str] = (),
    dry_run: bool = False,
    verify: bool = False,
    no_orphans: bool = False,
    pause: float = 0,
    config: str | None = None,
    quiet: bool = False,
) -> RunReport:
    """Sync these links, like ``trove get``. ``output`` is a base destination (the
    share's name becomes a subfolder); ``dest`` the exact folder, for one link.
    ``pause`` waits between links. ``quiet`` drops the progress output.

    Raises ConfigError (``trove.api.ConfigError``) for a bad config or arguments.
    """
    from trove.cli import run_get

    links = list(links)
    argv = ["get", *links]
    for flag, value in (("--output", output), ("--dest", dest), ("--name", name), ("--path", path),
                        ("--password", password)):
        if value:
            argv += [flag, value]
    for pattern in only:
        argv += ["--only", pattern]
    argv += [flag for flag, on in (("--dry-run", dry_run), ("--verify", verify), ("--no-orphans", no_orphans)) if on]
    if pause:
        argv += ["--pause", str(pause)]
    return _run(argv, config, quiet, run_get)


def sync(
    names: Iterable[str] = (),
    *,
    providers: Iterable[str] = (),
    dry_run: bool = False,
    recent_first: bool = False,
    no_orphans: bool = False,
    config: str | None = None,
    quiet: bool = False,
) -> RunReport:
    """Sync configured sites, like ``trove sync``: all enabled ones, or those whose
    names match and, with ``providers``, that sync from one of them. Another sync
    already running gives exit_code 4 and no shares."""
    from trove.cli import run_sync

    argv = ["sync", *names]
    for provider in providers:
        argv += ["--provider", provider]
    argv += [flag for flag, on in (("--dry-run", dry_run), ("--recent-first", recent_first),
                                   ("--no-orphans", no_orphans)) if on]
    return _run(argv, config, quiet, run_sync)


@dataclass(frozen=True)
class LinkInfo:
    """Where a link leads. ``final`` is the share link after any shortener (with its
    #fragment, so print it with ``split("#")[0]``); ``identity`` names the share the
    way ``SiteInfo.identity`` does, so two links to one share compare equal. When the
    link leads nowhere usable, those are None and ``error`` says why."""

    link: str
    final: str | None = None
    provider: str | None = None
    identity: str | None = None
    error: str | None = None


def identify(url: str, *, follow: bool = True) -> LinkInfo:
    """Where ``url`` leads, without listing anything. A shortener is walked by hand,
    one request per hop, and the share link it reaches is never fetched. With
    ``follow=False`` a shortener isn't walked, and comes back with an error."""
    from trove.http import resolve_shortener
    from trove.providers import PROVIDERS, is_provider_url, open_url

    final = url
    if not is_provider_url(url):
        if not follow:
            return LinkInfo(url, error="not a share link; follow it to see where it leads")
        try:
            final = resolve_shortener(url, is_provider_url)
        except Exception as e:  # a dead shortener, or the network
            return LinkInfo(url, error=f"{e.__class__.__name__}: {e}" if not isinstance(e, ValueError) else str(e))
    try:
        share = open_url(final)
    except Exception as e:
        return LinkInfo(url, final, error=str(e))
    try:
        provider = next((name for name, module in PROVIDERS.items() if module.matches(final)), share.provider_name)
        return LinkInfo(url, final, provider, share.identity)
    finally:
        share.close()


@dataclass(frozen=True)
class SiteInfo:
    """A site from the sites list, with what runs recorded about it. ``provider`` is
    the one it syncs from (forced, recognized from its link, or recorded). ``dest`` is
    the exact folder the site sets, if it does. With ``link_in_document``, the ``url``
    leads to a document holding the share link on those hosts, so it never leads to
    the share itself. ``identity`` and ``output`` (the folder
    it last synced into) are recorded by runs, so None until a sync succeeds. Either
    folder can be handed to ``open_folder``."""

    name: str
    url: str
    fallback: tuple[str, ...]
    enabled: bool
    provider: str | None
    identity: str | None
    output: str | None
    note: str | None
    dest: str | None = None
    link_in_document: tuple[str, ...] = ()


def sites(*, config: str | None = None) -> list[SiteInfo]:
    """The configured sites, in file order, disabled ones included."""
    from trove.cli import site_provider
    from trove.state import SiteState

    loaded = load_config(config)
    state = SiteState(loaded.state_path)
    out = []
    for site in loaded.sites:
        recorded = state.get(site.name)
        out.append(SiteInfo(site.name, site.url, tuple(site.fallback), site.enabled, site_provider(site, state),
                            recorded.get("identity"), recorded.get("output"), site.note, site.dest,
                            tuple(site.link_in_document)))
    return out


def config_path(config: str | None = None) -> str:
    """The config file trove would use (``config``, else $TROVE_CONFIG, else the
    default location), whether or not it exists. Its folder is a natural home for a
    helper's own records."""
    from trove.config import CONFIG_ENV, default_config_path

    return config or os.environ.get(CONFIG_ENV) or default_config_path()


@dataclass(frozen=True)
class FileInfo:
    name: str
    size: int
    mtime: float


class Folder:
    """Read access to one destination folder, through a local mount when there is one."""

    def __init__(self, fs, path: str):
        self._fs, self._path = fs, path

    def files(self) -> list[FileInfo]:
        """The regular files directly in the folder, by name."""
        found = self._fs.list_files(self._path)
        return [FileInfo(name, st.size, st.mtime) for name, st in sorted(found.items())]

    def read(self, name: str) -> bytes:
        with self._fs.open(self._fs.join(self._path, name), "rb") as fp:
            return fp.read()


@contextlib.contextmanager
def open_folder(dest: str, *, config: str | None = None) -> Iterator[Folder]:
    """A destination folder to read: a destination string like ``nas:/files/X``, an
    ``sftp://`` URL, a local path, or a ``SiteInfo.output``."""
    from trove.config import Destinations

    destinations = Destinations(load_config(config))
    try:
        fs, path = destinations.resolve(dest)
        yield Folder(fs, path)
    finally:
        destinations.close()
