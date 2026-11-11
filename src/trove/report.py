"""What a run did, per share, for other programs (``--json`` and ``trove.api``).

The shape is versioned. Within one version fields are only ever added, never
renamed or removed, so a caller can rely on what it reads today. Links are written
without their ``#fragment`` and with any password scrubbed, as everywhere else.

``status`` is one of:

* ``ok``: the share synced with no errors.
* ``failed``: something failed, a file or the share; see ``error`` and ``errors``.
* ``gone``: the link looks dead rather than temporarily failing.
* ``aborted``: the run stopped at this share (quota, outage, refused login).
* ``not_run``: the run stopped before reaching it.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = 1


@dataclass
class ShareReport:
    link: str
    status: str
    site: str | None = None
    error: str | None = None
    provider: str | None = None
    share_name: str | None = None
    output: str | None = None
    files: int | None = None
    downloaded: int = 0
    skipped: int = 0
    would_download: int = 0
    dates_changed: int = 0
    errors: int = 0
    orphans: int = 0
    deleted: int = 0
    renamed: int = 0


@dataclass
class RunReport:
    command: str
    exit_code: int
    dry_run: bool
    shares: list[ShareReport] = field(default_factory=list)
    error: str | None = None  # why the run as a whole didn't go ahead (config, another sync running)
    version: int = SCHEMA_VERSION

    @property
    def aborted(self) -> bool:
        return any(s.status == "aborted" for s in self.shares)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "command": self.command,
            "exit_code": self.exit_code,
            "dry_run": self.dry_run,
            "aborted": self.aborted,
            "error": self.error,
            "shares": [asdict(s) for s in self.shares],
        }


def share_report(link: str, code: int, result, error, gone: bool, site: str | None = None) -> ShareReport:
    """One share's outcome, from what _run_one returned."""
    from trove.cli import EXIT_ABORTED, EXIT_OK
    from trove.providers import _safe_url

    if code == EXIT_ABORTED:
        status = "aborted"
    elif gone:
        status = "gone"
    elif code == EXIT_OK:
        status = "ok"
    else:
        status = "failed"
    report = ShareReport(_safe_url(link), status, site=site, error=str(error)[:300] if error else None)
    if result is not None:
        report.downloaded, report.skipped = result.downloaded, result.skipped
        report.would_download, report.dates_changed = result.would_download, result.dates_changed
        report.errors, report.orphans = result.errors, len(result.orphans)
        report.deleted, report.renamed = result.deleted, result.renamed
        if result.info is not None:
            report.provider, report.share_name = result.info.provider, result.info.share_name
            report.output, report.files = result.info.output, result.info.files
    return report


def not_run(link: str, site: str | None = None) -> ShareReport:
    from trove.providers import _safe_url

    return ShareReport(_safe_url(link), "not_run", site=site)


def write_json(report: RunReport, path: str, stdout=None) -> None:
    data = json.dumps(report.to_dict(), ensure_ascii=False, indent=1)
    if path == "-":
        out = stdout or sys.stdout
        out.write(data + "\n")
        out.flush()
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write(data + "\n")
    import os

    os.replace(tmp, path)
