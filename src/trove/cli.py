"""The ``trove`` command.

Exit codes: 0 success, 1 something failed, 2 usage or config problem, 3 the run
stopped early because everything remaining would fail too.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import getpass
import os
import sys
import time
from dataclasses import dataclass, field

from trove import __version__
from trove.config import Config, Destinations, Site, _settings_from_url, default_config_path, expand_output, load_config
from trove.console import fmt_size, setup_console
from trove.engine import DEFAULT_DELETE_MAX_PCT, SyncOptions, SyncResult, judge_orphans, loose_key, sync_listing
from trove.errors import AbortRun, ConfigError, ShareError, TroveError
from trove.fs import RETRYABLE_ERRORS, keyring_user
from trove.lock import AlreadyRunning, RunLock
from trove.report import RunReport, not_run, share_report, write_json
from trove.naming import safe_path
from trove.providers import PROVIDERS, _safe_url, bare_links, is_gone_error, is_provider_url, open_url, select_share
from trove.state import ShareInfo, SiteState, describe_site

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_ABORTED, EXIT_RUNNING = 0, 1, 2, 3, 4


@dataclass
class ShareRequest:
    urls: list[str]
    password: str = ""
    output: str | None = None
    name: str | None = None
    dest: str | None = None
    subpath: str = ""
    rollover: str | None = None
    provider: str | None = None
    link_in_document: list[str] = field(default_factory=list)


def _read_secret_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fp:
            return fp.read().strip()
    except OSError as e:
        raise ConfigError(f"Could not read password file {path}: {e}") from e


def run_share(request: ShareRequest, options: SyncOptions, config: Config, destinations: Destinations) -> SyncResult:
    """Sync one share from link to finished tree."""
    share = select_share(request.urls, request.password, request.provider, request.link_in_document)
    try:
        return _sync_share(share, request, options, config, destinations)
    finally:
        share.close()


def _sync_share(share, request: ShareRequest, options: SyncOptions, config: Config, destinations: Destinations) -> SyncResult:
    for line in share.describe():
        print(line)

    # Connect before listing, which can take minutes, so destination errors show early.
    if request.dest:
        fs, base = destinations.resolve(request.dest)
    else:
        template = request.output or config.default_output
        if not template:
            raise ConfigError(
                "No destination: pass -o or --dest, or set default_output in the config "
                f"({config.path or default_config_path()})."
            )
        fs, base = destinations.resolve(expand_output(template, share.provider_name))
    print(f"Destination: {fs.describe(base)}")
    try:
        exists = fs.isdir(base)
    except RETRYABLE_ERRORS as e:
        raise ConfigError(f"Could not open destination: {e}") from e
    if not exists and not fs.is_remote and not fs.is_mount and not options.dry_run:
        # An unmounted drive or mount point must not silently fill the local disk.
        raise ConfigError(f"Local destination doesn't exist: {base} (create it first)")

    subpath = request.subpath.strip("/")
    print("Scanning directory tree...")
    try:
        listing = share.list(subpath)
    except TroveError:
        raise
    except Exception as e:
        raise ShareError(f"Listing failed: {e}", gone=is_gone_error(e)) from e

    print(f"  Files found: {len(listing.files)}")
    if listing.folders:
        print(f"  Folders found: {len(listing.folders)}")
    if not listing.files and not listing.folders:
        raise ShareError("The share is empty or the path doesn't exist.")
    if any(f.size is not None for f in listing.files):
        print(f"  Total size: {fmt_size(sum(f.size or 0 for f in listing.files))}")

    if request.dest:
        output_dir = base
        run_options = _with(options, subpath=subpath, reroot=bool(subpath), rollover=request.rollover)
    else:
        name = request.name or listing.share_name
        if not name:
            raise ShareError("The server gave no share name. Use --name or --dest.")
        local_name = safe_path(name, share.colon)
        if share.loose_names and not request.name:
            local_name = _existing_folder(fs, base, local_name)
        output_dir = fs.join(base, *local_name.split("/"))
        run_options = _with(options, subpath=subpath, reroot=False, rollover=request.rollover)

    print(f"\nOutput directory: {fs.describe(output_dir)}")
    info = ShareInfo(
        identity=share.identity,
        provider=share.provider_name,
        share_name=listing.share_name,
        files=len(listing.files),
        newest_mtime=max((f.mtime for f in listing.files if f.mtime), default=None),
        output=fs.canonical(output_dir),
    )
    result = sync_listing(listing, share, fs, output_dir, run_options)
    result.info = info
    return result


def _existing_folder(fs, base: str, name: str) -> str:
    """The folder already in ``base`` whose name loosely equals ``name``, if exactly
    one does. A name you chose is never changed; this is for a share's own name."""
    try:
        if not fs.isdir(base):
            return name
        _top, dirs, _files = next(iter(fs.walk(base)))
    except (StopIteration, *RETRYABLE_ERRORS):
        return name
    if name in dirs:
        return name
    key = loose_key(name)
    found = [d for d in dirs if loose_key(d) == key]
    return found[0] if len(found) == 1 else name


def _with(options: SyncOptions, **changes) -> SyncOptions:
    return SyncOptions(**{**options.__dict__, **changes})


def _options_from_args(args, config: Config) -> SyncOptions:
    markers = args.keep_marker if args.keep_marker else config.keep_markers
    return SyncOptions(
        dry_run=args.dry_run,
        dates_only=args.dates_only,
        verify=args.verify,
        seed_manifest=args.seed_manifest,
        no_orphans=args.no_orphans,
        delete_orphans=args.delete_orphans,
        delete_max_pct=args.delete_max_pct,
        keep_markers=tuple(markers),
        only=tuple(args.only or ()),
    )


def _destinations_from_args(args, config: Config) -> Destinations:
    return Destinations(
        config,
        {
            "identity_file": args.identity_file,
            "password_file": args.sftp_password_file,
            "known_hosts": args.known_hosts,
        },
        remote=getattr(args, "remote", None) or "auto",
    )


def _password_from_args(args) -> str:
    if args.password_file:
        return _read_secret_file(args.password_file)
    return args.password or os.environ.get("TROVE_SHARE_PASSWORD", "")


def cmd_get(args, config: Config) -> int:
    return _with_json(args, "get", lambda: run_get(args, config))


def run_get(args, config: Config) -> RunReport:
    """Sync the links given, over one connection per destination."""
    if args.dest and len(args.urls) > 1:
        raise ConfigError("--dest names one folder, so it can only take one link.")
    if args.fallback and len(args.urls) > 1:
        raise ConfigError("--fallback applies to a single link.")
    if args.name and len(args.urls) > 1:
        raise ConfigError("--name applies to a single link.")
    options = _options_from_args(args, config)
    destinations = _destinations_from_args(args, config)
    password = _password_from_args(args)
    urls = list(args.urls)
    if args.from_file:
        urls += _read_links(args.from_file)
    if not urls:
        raise ConfigError("No links given.")

    report = RunReport("get", EXIT_OK, options.dry_run)
    known = None if getattr(args, "no_site", False) else KnownSites(config)
    try:
        for i, url in enumerate(urls):
            if i and getattr(args, "pause", 0):
                time.sleep(args.pause)
            if len(urls) > 1:
                print(f"\n=== {_safe_url(url)} ===", flush=True)
            try:
                request = _get_request(url, args, config, password, known)
            except ConfigError as e:
                print(f"Error: {e}")
                report.shares.append(share_report(url, EXIT_FAILED, None, e, False))
                continue
            code, result, error = _run_one(request, options, config, destinations)
            gone = error is not None and is_gone_error(error)
            report.shares.append(share_report(url, code, result, error, gone))
            if code == EXIT_ABORTED:
                report.shares.extend(not_run(u) for u in urls[i + 1 :])
                break
    finally:
        destinations.close()
    report.exit_code = _worst([_code_of(s) for s in report.shares])
    return report


class KnownSites:
    """The configured sites, by the share each leads to, so ``get`` on a site's link
    (or its bare ID) syncs where that site does.

    A site is known by its last run's share (from the state file, which covers
    shorteners) and by its links. Sites syncing only a ``path`` of a share are left
    out: the link names the whole share."""

    def __init__(self, config: Config):
        self.by_key: dict[str, list[Site]] = {}
        state = SiteState(config.state_path)
        for site in config.sites:
            if site.path:
                continue
            keys = {_link_key(u) for u in (site.url, *site.fallback)}
            keys |= {_identity(u, site.provider) for u in (site.url, *site.fallback)}
            keys.add(state.get(site.name).get("identity"))
            for key in keys - {None}:
                self.by_key.setdefault(key, []).append(site)

    def find(self, url: str, provider: str | None) -> tuple[list[Site], str]:
        """The sites this link or bare ID leads to, and the full link to use."""
        if provider or is_provider_url(url):
            links = [url]
        else:
            links = bare_links(url) or [url]
        for link in links:
            found = self.by_key.get(_link_key(link), []) or self.by_key.get(_identity(link, provider), [])
            if found:
                return _unique(found), link
        return [], url


def _unique(sites: list[Site]) -> list[Site]:
    seen, out = set(), []
    for site in sites:
        if site.name not in seen:
            seen.add(site.name)
            out.append(site)
    return out


def _link_key(url: str) -> str:
    return "link:" + url.strip().split("#", 1)[0].rstrip("/").lower().removeprefix("https://").removeprefix("http://")


def _identity(url: str, provider: str | None = None) -> str | None:
    """A link's share identity, worked out from the link alone (no network)."""
    try:
        share = open_url(url, "", provider)
    except Exception:
        return None
    try:
        return share.identity
    finally:
        share.close()


def _get_request(url: str, args, config: Config, password: str, known: "KnownSites | None") -> ShareRequest:
    """What ``get`` runs for one link: a configured site's settings when the link
    leads to one, with the command line winning, else just the command line."""
    sites, link = known.find(url, args.provider) if known else ([], url)
    if not sites and not args.provider and not is_provider_url(url) and bare_links(url):
        raise ConfigError(
            f"{_safe_url(url)} isn't a link, and no configured site uses it. "
            "For a bare ID, name the provider with --provider."
        )
    if len(sites) > 1 and not (args.output or args.dest):
        names = ", ".join(repr(s.name) for s in sites)
        raise ConfigError(f"Several sites use this link ({names}); run one with trove sync NAME, or pass -o or --dest.")
    if len(sites) != 1:
        return ShareRequest(
            urls=[link, *(args.fallback or [])], password=password, output=args.output, name=args.name,
            dest=args.dest, subpath=args.path or "", rollover=args.rollover, provider=args.provider,
        )
    site = sites[0]
    print(f"Known site: {site.name}")
    site_request = _site_request(site, config, args)
    return ShareRequest(
        urls=[link, *(args.fallback or [])],
        password=password or site_request.password,
        output=args.output or (None if args.dest else site_request.output),
        name=args.name or (None if args.dest else site.folder),
        dest=args.dest or (None if args.output else site.dest),
        subpath=args.path or "",
        rollover=args.rollover or site.rollover,
        provider=args.provider or site.provider,
    )


def _code_of(share) -> int:
    return {"ok": EXIT_OK, "aborted": EXIT_ABORTED, "not_run": EXIT_OK}.get(share.status, EXIT_FAILED)


def _with_json(args, command: str, run) -> int:
    """Run, and write the report where --json says. With ``--json -`` the report
    goes to stdout and everything else to stderr, so stdout is only JSON."""
    path = getattr(args, "json", None)
    real_stdout = sys.stdout
    try:
        if path == "-":
            with contextlib.redirect_stdout(sys.stderr):
                report = run()
        else:
            report = run()
    except ConfigError as e:
        if path:
            write_json(RunReport(command, EXIT_USAGE, bool(getattr(args, "dry_run", False)), error=str(e)), path, real_stdout)
        raise
    if path:
        write_json(report, path, real_stdout)
    return report.exit_code


def _read_links(path: str) -> list[str]:
    fp = sys.stdin if path == "-" else open(path, encoding="utf-8")
    try:
        return [line.strip() for line in fp if line.strip() and not line.startswith("#")]
    finally:
        if fp is not sys.stdin:
            fp.close()


def _run_one(request: ShareRequest, options: SyncOptions, config: Config, destinations: Destinations):
    """Run one share and return ``(exit code, result or None, error or None)``."""
    try:
        result = run_share(request, options, config, destinations)
    except AbortRun as e:
        print(f"Stopping the run: {e}")
        return EXIT_ABORTED, None, e
    except (ShareError, ConfigError) as e:
        print(f"Error: {e}")
        return EXIT_FAILED, None, e
    except RETRYABLE_ERRORS as e:
        print(f"Error: {e.__class__.__name__}: {e}")
        return EXIT_FAILED, None, e
    return (EXIT_OK if result.ok else EXIT_FAILED), result, None


def _worst(codes: list[int]) -> int:
    return max(codes, default=EXIT_OK)


def _site_matches(site: Site, patterns: list[str]) -> bool:
    name = site.name.lower()
    return any(fnmatch.fnmatch(name, p.lower()) or p.lower() in name for p in patterns)


def _site_request(site: Site, config: Config, args) -> ShareRequest:
    password = _read_secret_file(config.resolve_relative(site.password_file)) if site.password_file else ""
    return ShareRequest(
        urls=[site.url, *site.fallback],
        password=password,
        output=args.output or site.output,
        name=site.folder,
        dest=site.dest,
        subpath=site.path or "",
        rollover=site.rollover,
        provider=site.provider,
        link_in_document=site.link_in_document,
    )


def site_provider(site: Site, state: SiteState) -> str | None:
    """The provider a site syncs from, without going online: the one it forces, else
    the one recognizing its link, else (for a shortener or a document) the one its
    last run recorded. None when a site like that hasn't run yet."""
    if site.provider:
        return site.provider.lower()
    if not site.link_in_document:
        for name, module in PROVIDERS.items():
            if module.matches(site.url):
                return name
    return state.get(site.name).get("provider")


@dataclass
class Selection:
    sites: list[Site]
    disabled: int = 0  # matching sites left out because they're disabled
    unknown: int = 0  # left out because their provider isn't known yet


def _select_sites(
    config: Config, names: list[str], providers: list[str] | None = None, state: SiteState | None = None
) -> Selection:
    """Enabled sites matching ``names`` and syncing from one of ``providers``. A
    disabled site runs only when named in full."""
    exact = {n.lower() for n in names}
    matching = [s for s in config.sites if not names or _site_matches(s, names)]
    chosen = [s for s in matching if s.enabled or s.name.lower() in exact]
    selection = Selection(chosen, disabled=len(matching) - len(chosen))
    if providers:
        unknown = set(p.lower() for p in providers) - set(PROVIDERS)
        if unknown:
            raise ConfigError(f"Unknown provider {', '.join(sorted(unknown))} (known: {', '.join(PROVIDERS)})")
        wanted = {p.lower() for p in providers}
        found = {s.name: site_provider(s, state or SiteState(None)) for s in chosen}
        selection.sites = [s for s in chosen if found[s.name] in wanted]
        selection.unknown = sum(1 for s in chosen if found[s.name] is None)
    return selection


def lock_path(config: Config) -> str:
    """Beside the state file, so every run against one site list shares the lock."""
    base = config.state_path or os.path.join(os.path.dirname(config.path or default_config_path()), "state.json")
    return base + ".lock"


def cmd_sync(args, config: Config) -> int:
    return _with_json(args, "sync", lambda: run_sync(args, config))


def run_sync(args, config: Config) -> RunReport:
    """Sync the configured sites, one pass at a time."""
    try:
        with RunLock(lock_path(config)):
            return _sync_sites(args, config)
    except AlreadyRunning as e:
        print(e)
        return RunReport("sync", EXIT_RUNNING, bool(getattr(args, "dry_run", False)), error=str(e))


def recent_first(sites: list[Site], state: SiteState) -> list[Site]:
    """Newest content first, from what runs recorded. Sites never run yet go first,
    since nothing is known about them; the rest keep their order among equals."""
    def key(site: Site):
        newest = state.get(site.name).get("newest_mtime")
        return (newest is not None, -(newest or 0))

    return sorted(sites, key=key)


def _sync_sites(args, config: Config) -> RunReport:
    state = SiteState(config.state_path)
    selection = _select_sites(config, args.names, getattr(args, "providers", None), state)
    sites, disabled = selection.sites, selection.disabled
    if selection.unknown:
        print(f"{selection.unknown} site(s) left out: their link is a shortener or a document, and no run has "
              "recorded its provider yet. Name them to run them once.")
    if not sites:
        raise ConfigError(f"No configured site matches ({config.sites_path or default_config_path()}).")

    options = _options_from_args(args, config)
    destinations = _destinations_from_args(args, config)
    if getattr(args, "recent_first", False):
        sites = recent_first(sites, state)
    results: list[tuple[str, int, bool]] = []
    report = RunReport("sync", EXIT_OK, options.dry_run)
    # Sharing is judged across every site a full run would sync, so a folder shared
    # with a site left out of this run is never judged without that site's files.
    everyone = [s for s in config.sites if s.enabled or s in sites]
    shared = _shared_outputs(everyone, state)
    claims: dict[str, list[tuple[str, object]]] = {}
    deferred: set[str] = set()
    outputs: dict[str, list[str]] = {}
    try:
        for site in sites:
            print(f"\n=== {site.name} ===", flush=True)
            defer = site.name in shared or options.delete_orphans
            site_options = _with(options, defer_orphans=True) if defer else options
            try:
                code, result, error = _run_one(_site_request(site, config, args), site_options, config, destinations)
            except ConfigError as e:
                print(f"Error: {e}")
                code, result, error = EXIT_FAILED, None, e
            if result is not None and result.claim is not None:
                key = result.claim.fs.describe(result.claim.output_dir)
                claims.setdefault(key, []).append((site.name, result.claim))
                deferred.add(site.name)
            if result is not None and result.info is not None and result.info.output:
                outputs.setdefault(result.info.output, []).append(site.name)
            gone = False
            if result is not None and result.info is not None:
                for warning in state.record_success(site.name, result.info):
                    print(f"WARNING: {warning}")
            elif isinstance(error, (ShareError, *RETRYABLE_ERRORS)):
                # A config problem says nothing about the site, so it isn't recorded.
                gone = is_gone_error(error)
                state.record_failure(site.name, str(error)[:200], gone)
            state.save()
            results.append((site.name, code, gone))
            report.shares.append(share_report(site.url, code, result, error, gone, site=site.name))
            if code == EXIT_ABORTED:
                print("Stopping: the remaining sites would fail the same way.")
                done = {r.site for r in report.shares}
                report.shares.extend(not_run(s.url, s.name) for s in sites if s.name not in done)
                break
        judged = _judge_shared(claims, shared, options, {s.name for s in sites})
        results.extend((name, code, gone) for name, code, gone, _orphans in judged)
    finally:
        destinations.close()

    for output, names in outputs.items():
        if len(names) > 1 and not all(n in deferred for n in names):
            print(
                f"\nWARNING: {', '.join(names)} all sync into {output}, so each one's orphans included the others' "
                "files. Now that this is recorded, the next sync checks their orphans together."
            )

    _print_sync_summary(report, judged, options.dry_run)
    if disabled:
        print(f"{disabled} disabled site(s) left out.")
    if any(gone for _, _, gone in results):
        print("Set 'enabled: false' on a dead site to retire it; 'trove sites --gone' lists them.")
    report.exit_code = _worst([code for _, code, _ in results])
    return report


SUMMARY_NAME_WIDTH = 40


def _print_sync_summary(report: RunReport, judged: list[tuple[str, int, bool, int]], dry_run: bool) -> None:
    """One line per site with what it fetched, then totals, so a long run's results
    don't have to be found in its output."""
    ran = [s for s in report.shares if s.status != "not_run"]
    words = {"ok": "ok", "gone": "LINK GONE", "aborted": "STOPPED THE RUN", "failed": "failed"}
    rows: list[tuple[str, str, list[int | None]]] = []
    for s in ran:
        listed = s.files is not None
        counts = [s.would_download if dry_run else s.downloaded, s.dates_changed, s.errors, s.orphans]
        rows.append((s.site or s.link, words.get(s.status, s.status), counts if listed else [None] * 4))
    for label, code, _gone, orphans in judged:
        rows.append((label, "ok" if code == EXIT_OK else "failed", [None, None, None, orphans]))

    print("\n=== Summary ===")
    if rows:
        width = min(max(len(name) for name, _, _ in rows), SUMMARY_NAME_WIDTH)
        heads = ["Would get" if dry_run else "New", "Dates", "Errors", "Orphans"]
        print(f"{'Site':<{width}}  {'Result':<15}" + "".join(f" {h:>9}" for h in heads))
        for name, word, counts in rows:
            if len(name) > width:
                name = name[: width - 3] + "..."
            cells = "".join(f" {'' if c is None else c:>9}" for c in counts)
            print(f"{name:<{width}}  {word:<15}{cells}".rstrip())
        total = [sum(c[i] or 0 for _, _, c in rows) for i in range(4)]
        print(f"Total across {len(ran)} site(s): {total[0]} {'to download' if dry_run else 'downloaded'}, "
              f"{total[1]} date(s) {'to change' if dry_run else 'changed'}, {total[2]} error(s), {total[3]} orphan(s).")

    skipped = sum(1 for s in report.shares if s.status == "not_run")
    if skipped:
        stopper = next((s for s in report.shares if s.status == "aborted"), None)
        why = f": the run stopped at {stopper.site}. {stopper.error}" if stopper and stopper.error else ""
        print(f"{skipped} site(s) not attempted{why}")


def _shared_outputs(sites: list[Site], state: SiteState) -> dict[str, str]:
    """Sites whose last recorded output folder is also another of ``sites``',
    mapped to that folder. Their orphan checks run together at the end."""
    by_output: dict[str, list[str]] = {}
    for site in sites:
        output = state.get(site.name).get("output")
        if output:
            by_output.setdefault(output, []).append(site.name)
    return {name: output for output, names in by_output.items() if len(names) > 1 for name in names}


def _judge_shared(
    claims: dict, shared: dict[str, str], options: SyncOptions, running: set[str] | None = None
) -> list[tuple[str, int, bool, int]]:
    """One orphan check per folder that several sites sync into, as (label, exit code,
    gone, orphans). A folder shared with a site outside this run (``running``) isn't
    checked, since that site's files would all look like orphans."""
    outcomes = []
    for output, group in claims.items():
        names = [name for name, _claim in group]
        expected = [n for n, out in shared.items() if out == output]
        print(f"\n=== Orphans in {output} ({', '.join(names)}) ===")
        absent = [n for n in expected if running is not None and n not in running]
        if absent:
            print(f"  Not checked: {', '.join(absent)} also sync{'s' if len(absent) == 1 else ''} here "
                  "and aren't in this run. A run including them checks this folder.")
            continue
        missing = [n for n in expected if n not in names]
        group_options = options
        if missing:
            # Without them, their files would look like orphans.
            print(f"  {', '.join(missing)} didn't list, so their files can't be told apart; nothing is deleted.")
            group_options = _with(options, delete_orphans=False)
        result = SyncResult()
        judge_orphans([claim for _name, claim in group], group_options, result)
        if missing and options.delete_orphans:
            result.errors += 1
        if len(names) > 1 or not result.ok:
            outcomes.append((f"orphans: {', '.join(names)}", EXIT_OK if result.ok else EXIT_FAILED, False,
                             len(result.orphans)))
    return outcomes


STATIC_AFTER = 2 * 365 * 86400  # newest file this old: a display cue, never a reason to skip


def _age(seconds: float) -> str:
    if seconds < 0:
        return "future"  # a file dated ahead, which some uploads are
    days = seconds / 86400
    if days < 1:
        return f"{int(seconds // 3600)}h"
    if days < 60:
        return f"{int(days)}d"
    if days < 730:
        return f"{int(days // 30.4)}mo"
    return f"{days / 365.25:.1f}y"


def print_ages(sites: list[Site], state: SiteState, now: float | None = None) -> None:
    """When each site last gained content, newest first, from what runs recorded."""
    now = now if now is not None else time.time()
    known, unknown = [], []
    for site in sites:
        newest = state.get(site.name).get("newest_mtime")
        (known if newest else unknown).append((newest, site))
    known.sort(key=lambda pair: -pair[0])
    width = max((len(s.name) for _n, s in known), default=0)
    for newest, site in known:
        info = state.get(site.name)
        mark = "  static" if now - newest > STATIC_AFTER else ""
        files = info.get("files")
        print(f"{_age(now - newest):>6}  {time.strftime('%Y-%m-%d', time.localtime(newest))}  "
              f"{site.name:<{width}}  {files if files is not None else '?':>7} files{mark}")
    static = sum(1 for newest, _s in known if now - newest > STATIC_AFTER)
    print(f"\n{len(known)} with a known date ({static} static: nothing new in 2+ years), {len(unknown)} not run yet or never listed.")


def cmd_sites(args, config: Config) -> int:
    if not config.sites:
        print(f"No sites configured ({config.sites_path or default_config_path()}).")
    state = SiteState(config.state_path)
    if args.ages:
        print_ages([s for s in config.sites if s.enabled], state)
        return EXIT_OK
    for site in config.sites:
        info = state.get(site.name)
        if args.gone and not (info.get("gone") and info.get("failures")):
            continue
        status = "disabled" if not site.enabled else describe_site(info)
        print(f"{site.name}  [{status}]")
        if not args.brief:
            print(f"  url: {site.url.split('#', 1)[0]}")
            for fallback in site.fallback:
                print(f"  fallback: {fallback.split('#', 1)[0]}")
            for key in ("provider", "output", "folder", "dest", "path", "rollover", "note"):
                value = getattr(site, key)
                if value:
                    print(f"  {key}: {value}")
    return EXIT_OK


def cmd_doctor(args, config: Config) -> int:
    problems = 0
    print(f"trove {__version__}, Python {sys.version.split()[0]}, {sys.platform}")
    print(f"Config: {config.path or default_config_path() + ' (not found)'}")
    folder = os.path.dirname(config.path or default_config_path())
    for label, path in (("Sites file", config.sites_path or os.path.join(folder, "sites.yaml")),
                        ("State file", config.state_path or os.path.join(folder, "state.json"))):
        print(f"{label}: {path}{'' if os.path.isfile(path) else ' (not found)'}")
    print(f"Sites: {len(config.sites)}; default output: {config.default_output or '(not set)'}")
    destinations = _destinations_from_args(args, config)
    try:
        for name in config.destinations:
            try:
                fs, path = destinations.resolve(f"{name}:")
                found = fs.isdir(path)
                print(f"  {name}: {fs.describe(path)} {'ok' if found else 'NOT FOUND'}")
                problems += 0 if found else 1
            except (TroveError, *RETRYABLE_ERRORS) as e:
                print(f"  {name}: FAILED ({e})")
                problems += 1
    finally:
        destinations.close()
    account = _mediafire_account_line()
    print(account)
    problems += "FAILED" in account
    return EXIT_FAILED if problems else EXIT_OK


def _set_mediafire_login() -> int:
    import keyring

    from trove.providers import mediafire

    email = input("MediaFire account email: ").strip()
    password = getpass.getpass("MediaFire password: ")
    if not email or not password:
        raise ConfigError("Both the email and the password are needed.")
    keyring.set_password(mediafire.KEYRING_SERVICE, "email", email)
    keyring.set_password(mediafire.KEYRING_SERVICE, "password", password)
    print("Stored in the OS keyring. MediaFire downloads will use the account; check it with 'trove doctor'.")
    return EXIT_OK


def _mediafire_account_line() -> str:
    """How MediaFire downloads will go, for doctor. Logs in only when an account is set up."""
    from trove.providers import mediafire
    from trove.http import new_session

    found = mediafire.find_credentials()
    if not found:
        return "MediaFire: no account (anonymous downloads); 'trove set-password mediafire' adds one"
    try:
        info = mediafire.MediaFireAccount(*found, new_session()).info()
    except (TroveError, *RETRYABLE_ERRORS) as e:
        return f"MediaFire: account login FAILED ({e})"
    return f"MediaFire: account ok ({'premium' if info.get('premium') == 'yes' else 'free'})"


def cmd_set_password(args, config: Config) -> int:
    target = args.target
    if target == "mediafire" and target not in config.destinations:
        return _set_mediafire_login()
    spec = config.destinations.get(target)
    if spec is not None:
        if not spec.get("url"):
            raise ConfigError(f"Destination {target!r} isn't an SFTP destination.")
        settings, _ = _settings_from_url(spec["url"], spec)
    else:
        settings, _ = _settings_from_url(target)
    import keyring

    keyring.set_password(
        settings.keyring_service,
        keyring_user(settings),
        getpass.getpass(f"Password for {settings.label}: "),
    )
    print("Stored in the OS keyring.")
    return EXIT_OK


def _add_sync_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", metavar="FILE",
                        help="Write a summary of every share as JSON ('-' for stdout, with progress on stderr)")
    parser.add_argument("-o", "--output", help="Base destination; the share's name becomes a subfolder. "
                        "Forms: name:/sub/path, sftp://user@host:port/path, or a local path. {provider} is replaced.")
    parser.add_argument("--only", action="append", metavar="GLOB", help="Only fetch files whose share path matches (repeatable, case-insensitive)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen and write nothing")
    parser.add_argument("--dates-only", action="store_true", help="Only fix file and folder dates")
    parser.add_argument("--verify", action="store_true", help="Also re-download files that changed, by the server's recorded tag or, where the site gives hashes, by reading each file back")
    parser.add_argument("--seed-manifest", action="store_true", help="Record current server tags for existing files, without downloading")
    parser.add_argument("--no-orphans", action="store_true", help="Skip the orphan check")
    parser.add_argument("--delete-orphans", action="store_true", help="Delete reported orphans and folders left empty")
    parser.add_argument("--delete-max-pct", type=float, default=DEFAULT_DELETE_MAX_PCT, metavar="PCT",
                        help="Refuse to delete more than this share of a folder's local files (default: %(default)g)")
    parser.add_argument("--keep-marker", action="append", metavar="TEXT", help="Names containing this are never orphans (repeatable; replaces the config's list)")
    parser.add_argument("--identity-file", help="SSH private key for an SFTP destination")
    parser.add_argument("--sftp-password-file", help="File holding the SFTP password")
    parser.add_argument("--known-hosts", help="File for trusted SSH host keys")
    parser.add_argument("--remote", choices=("auto", "on", "off"), default="auto",
                        help="For an SFTP destination with a local mount: auto writes through the mount when "
                        "it's there (the default), on always uses SFTP, off insists on the mount")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trove", description="Download and sync files from public file-share links.")
    parser.add_argument("--version", action="version", version=f"trove {__version__}")
    parser.add_argument("--config", help=f"Config file (default: ${'TROVE_CONFIG'}, then {default_config_path()})")
    sub = parser.add_subparsers(dest="command", required=True)

    get = sub.add_parser("get", help="Sync one or more links")
    get.add_argument("urls", nargs="*", metavar="URL", help="Share links. Quote them, since # starts a shell comment.")
    get.add_argument("--from-file", metavar="FILE", help="Read links from a file, one per line ('-' for stdin)")
    get.add_argument("--fallback", action="append", metavar="URL", help="Another link to try if the first fails (repeatable)")
    get.add_argument("--provider", help="Force a provider instead of detecting it from the link")
    get.add_argument("--dest", help="The exact folder to mirror into. With --path, that subfolder's contents land directly in it.")
    get.add_argument("--name", help="Subfolder under --output instead of the share's own name")
    get.add_argument("-p", "--path", help="Only this subfolder of the share")
    get.add_argument("--password", help="Share password")
    get.add_argument("--password-file", help="File holding the share password")
    get.add_argument("--rollover", metavar="DIR", help="Archive folder the server moves old top-level folders into")
    get.add_argument("--no-site", action="store_true",
                     help="Don't look the link up among the configured sites; use only the command line")
    get.add_argument("--pause", type=float, default=0, metavar="SECONDS",
                     help="Wait this long between links, for sites that ban fast clients")
    _add_sync_flags(get)
    get.set_defaults(func=cmd_get)

    sync = sub.add_parser("sync", help="Sync the sites in the config")
    sync.add_argument("names", nargs="*", metavar="NAME", help="Only sites matching these (substring or wildcard)")
    sync.add_argument("--provider", dest="providers", action="append", metavar="PROVIDER",
                      help=f"Only sites syncing from this provider (repeatable; {', '.join(PROVIDERS)})")
    sync.add_argument("--recent-first", action="store_true",
                      help="Sync the sites whose content changed most recently first (from recorded runs)")
    _add_sync_flags(sync)
    sync.set_defaults(func=cmd_sync)

    sites = sub.add_parser("sites", help="List the configured sites and how each is doing")
    sites.add_argument("--brief", action="store_true", help="One line per site")
    sites.add_argument("--gone", action="store_true", help="Only sites whose link looks dead")
    sites.add_argument("--ages", action="store_true",
                       help="When each enabled site last gained content, newest first")
    sites.set_defaults(func=cmd_sites)

    doctor = sub.add_parser("doctor", help="Check the config and destinations")
    doctor.add_argument("--identity-file")
    doctor.add_argument("--sftp-password-file")
    doctor.add_argument("--known-hosts")
    doctor.set_defaults(func=cmd_doctor)

    password = sub.add_parser("set-password", help="Store an SFTP password, or a MediaFire login, in the OS keyring")
    password.add_argument("target", help="A destination name, sftp://user@host:port, or 'mediafire'")
    password.set_defaults(func=cmd_set_password)
    return parser


def main(argv: list[str] | None = None) -> None:
    setup_console()
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        code = args.func(args, config)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        code = EXIT_USAGE
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        code = 130
    sys.exit(code)
