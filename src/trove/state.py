"""What trove has learned about each site, kept apart from the hand-edited site list.

The site list says what you want synced. This file records what happened: where a
link currently leads, when it last worked, and how long it has been failing. It is
written by real runs and dry runs alike, because it lives beside the config rather
than at the destination. Losing it loses only history.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass


@dataclass
class ShareInfo:
    """What a successful listing found."""

    identity: str
    provider: str
    share_name: str | None
    files: int
    newest_mtime: float | None
    output: str | None = None  # the folder it synced into, as the destination describes it


class SiteState:
    def __init__(self, path: str | None):
        self.path = path
        self.sites: dict[str, dict] = {}
        if path:
            try:
                with open(path, encoding="utf-8") as fp:
                    data = json.load(fp)
                self.sites = data.get("sites", {}) if isinstance(data, dict) else {}
            except FileNotFoundError:
                pass
            except ValueError as e:
                print(f"Warning: ignoring unreadable state file {path}: {e}")

    def get(self, name: str) -> dict:
        return self.sites.get(name, {})

    def record_success(self, name: str, info: ShareInfo, now: float | None = None) -> list[str]:
        """Update a site and return warnings about anything that moved."""
        now = time.time() if now is None else now
        before = self.sites.get(name, {})
        warnings = []
        if before.get("provider") and before["provider"] != info.provider:
            warnings.append(
                f"this link moved from {before['provider']} to {info.provider}. "
                "Set 'output' or 'dest' on the site if its files should stay where they are."
            )
        elif before.get("identity") and before["identity"] != info.identity:
            warnings.append("this link now leads to a different share than last time.")
        if before.get("share_name") and info.share_name and before["share_name"] != info.share_name:
            warnings.append(
                f"the share was renamed from {before['share_name']!r} to {info.share_name!r}. "
                "Set 'folder' on the site if its files should stay where they are."
            )
        self.sites[name] = {
            "identity": info.identity,
            "provider": info.provider,
            "share_name": info.share_name,
            "files": info.files,
            "newest_mtime": info.newest_mtime,
            "output": info.output,
            "last_ok": now,
        }
        return warnings

    def record_failure(self, name: str, error: str, gone: bool, now: float | None = None) -> None:
        now = time.time() if now is None else now
        site = self.sites.setdefault(name, {})
        site["last_error"] = error
        site["gone"] = gone
        site["failures"] = site.get("failures", 0) + 1
        site.setdefault("failing_since", now)

    def save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump({"sites": self.sites}, fp, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"Warning: could not save site state: {e}")


def _date(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "?"


def describe_site(site: dict) -> str:
    """One line on a site's health."""
    if not site:
        return "not run yet"
    if site.get("failures"):
        kind = "GONE" if site.get("gone") else "failing"
        since = _date(site.get("failing_since"))
        worked = f", last worked {_date(site['last_ok'])}" if site.get("last_ok") else ""
        return f"{kind} since {since} ({site['failures']} runs){worked}"
    newest = _date(site.get("newest_mtime"))
    return f"ok {_date(site.get('last_ok'))}: {site.get('provider')}, {site.get('files')} files, newest {newest}"
