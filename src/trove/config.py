"""User configuration and destination resolution.

Every site, destination and credential location comes from the config file or the
command line. The code knows no hosts, drive letters or paths.

    default_output: "nas:/files/{provider}"
    keep_markers: ["(kept)"]
    destinations:
      nas:
        url: "sftp://user@host.example:22/"
        identity_file: "~/.ssh/id_ed25519"   # optional
        password_file: "~/secrets/nas.txt"   # optional
        keyring_service: "trove-downloader"  # optional
        known_hosts: "~/.ssh/known_hosts"    # optional
      scratch:
        path: "/tmp/trove"
    sites:
      - name: "Example"
        url: "https://short.example/abc"
        fallback: ["https://cloud.example/s/token"]
        password_file: "secrets/example.txt"
        folder: "Group/Example"
"""

from __future__ import annotations

import getpass
import os
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

import yaml
from platformdirs import user_config_dir

from trove.errors import ConfigError
from trove.fs import LocalFS, MountFS, SFTPFS, SFTPSettings

CONFIG_ENV = "TROVE_CONFIG"
APP_NAME = "trove-downloader"
_ALIAS_RE = re.compile(r"^([A-Za-z0-9_-]{2,}):(.*)$", re.DOTALL)


def default_config_path() -> str:
    return os.path.join(user_config_dir(APP_NAME, appauthor=False), "config.yaml")


@dataclass
class Site:
    name: str
    url: str
    fallback: list[str] = field(default_factory=list)
    password_file: str | None = None
    output: str | None = None
    folder: str | None = None
    dest: str | None = None
    path: str | None = None
    rollover: str | None = None
    provider: str | None = None
    enabled: bool = True
    note: str | None = None
    # Hosts whose link to take from the document the url leads to (see trove.documents).
    link_in_document: list[str] = field(default_factory=list)


@dataclass
class Config:
    path: str | None = None
    default_output: str | None = None
    keep_markers: list[str] = field(default_factory=list)
    destinations: dict[str, dict] = field(default_factory=dict)
    sites: list[Site] = field(default_factory=list)
    sites_path: str | None = None
    state_path: str | None = None

    def resolve_relative(self, path: str) -> str:
        """Expand ~ and resolve a relative path against the config file's folder."""
        expanded = os.path.expanduser(path)
        if os.path.isabs(expanded) or not self.path:
            return expanded
        return os.path.join(os.path.dirname(os.path.abspath(self.path)), expanded)


def load_config(path: str | None = None) -> Config:
    """Load the config. Only the default location may be missing."""
    explicit = path or os.environ.get(CONFIG_ENV)
    location = explicit or default_config_path()
    try:
        with open(location, encoding="utf-8") as fp:
            data = yaml.safe_load(fp) or {}
    except FileNotFoundError:
        if explicit:
            raise ConfigError(f"Config file not found: {location}") from None
        return Config()
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"Could not read config {location}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"Config {location} must be a mapping")

    config = Config(
        path=location,
        default_output=data.get("default_output"),
        keep_markers=list(data.get("keep_markers") or []),
        destinations=dict(data.get("destinations") or {}),
    )
    config.state_path = config.resolve_relative(data.get("state_file") or "state.json")
    sites_file = data.get("sites_file")
    config.sites_path = config.resolve_relative(sites_file or "sites.yaml")
    config.sites = _parse_sites(data.get("sites"), location)
    config.sites += _load_sites_file(config.sites_path, required=bool(sites_file))
    _check_unique(config.sites)
    return config


def _load_sites_file(path: str, required: bool) -> list[Site]:
    """Read the site list. It is a separate file so it can move between machines
    while each machine keeps its own destinations and credentials."""
    try:
        with open(path, encoding="utf-8") as fp:
            data = yaml.safe_load(fp) or {}
    except FileNotFoundError:
        if required:
            raise ConfigError(f"Sites file not found: {path}") from None
        return []
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"Could not read sites file {path}: {e}") from e
    raw = data.get("sites") if isinstance(data, dict) else data
    return _parse_sites(raw, path)


def _as_list(value) -> list[str]:
    if not value:
        return []
    return [value] if isinstance(value, str) else [str(v) for v in value]


def _parse_sites(raw_sites, location: str) -> list[Site]:
    sites = []
    for raw in raw_sites or []:
        try:
            fallback = raw.get("fallback") or []
            sites.append(
                Site(
                    name=raw["name"],
                    url=raw["url"],
                    fallback=[fallback] if isinstance(fallback, str) else list(fallback),
                    password_file=raw.get("password_file"),
                    output=raw.get("output"),
                    folder=raw.get("folder"),
                    dest=raw.get("dest"),
                    path=raw.get("path"),
                    rollover=raw.get("rollover"),
                    provider=raw.get("provider"),
                    enabled=raw.get("enabled", True),
                    note=raw.get("note"),
                    link_in_document=_as_list(raw.get("link_in_document")),
                )
            )
        except (KeyError, AttributeError, TypeError) as e:
            # The entry can hold a link key, so name only its position.
            raise ConfigError(f"Bad site entry #{len(sites) + 1} in {location}: {e}") from e
    return sites


def _check_unique(sites: list[Site]) -> None:
    seen: set[str] = set()
    for site in sites:
        key = site.name.lower()
        if key in seen:
            raise ConfigError(f"Two sites are named {site.name!r}; state is tracked by name.")
        seen.add(key)


def _settings_from_url(url: str, options: dict | None = None) -> tuple[SFTPSettings, str]:
    u = urlparse(url)
    if u.scheme.lower() != "sftp" or not u.hostname:
        raise ConfigError(f"Not an sftp:// URL: {u.scheme}://{u.hostname or ''}")
    if u.password:
        raise ConfigError(
            "Don't put a password in an sftp:// URL. Use password_file, "
            "the environment or the keyring."
        )
    options = options or {}
    settings = SFTPSettings(
        host=u.hostname,
        port=u.port or 22,
        username=unquote(u.username) if u.username else getpass.getuser(),
        identity_file=options.get("identity_file"),
        password_file=options.get("password_file"),
        keyring_service=options.get("keyring_service") or "trove-downloader",
        known_hosts=options.get("known_hosts"),
    )
    return settings, unquote(u.path) or "/"


class Destinations:
    """Resolve destination strings and share one connection per SFTP server."""

    def __init__(self, config: Config, sftp_options: dict | None = None, remote: str = "auto"):
        """``remote`` picks between an SFTP destination and its local mount (a
        destination's ``mounts``): ``auto`` writes through the mount when it's there,
        ``on`` always uses SFTP, and ``off`` insists on the mount."""
        self.config = config
        self.sftp_options = {k: v for k, v in (sftp_options or {}).items() if v}
        self.remote = remote
        self._local = LocalFS()
        self._sftp: dict[tuple, SFTPFS] = {}
        self._mounts: dict[tuple, MountFS] = {}

    def resolve(self, dest: str):
        """Return ``(fs, path)`` for a destination string.

        Accepted forms are ``name:/sub/path`` (from the config),
        ``sftp://user@host:port/path``, and any local path. Single-letter prefixes
        are left alone so Windows drive paths stay local paths.
        """
        if dest.lower().startswith("sftp://"):
            settings, path = _settings_from_url(dest, self.sftp_options)
            mounted = self._mounted(settings, path)
            if mounted:
                return mounted
            return self._open_sftp(dest, self.sftp_options)

        match = _ALIAS_RE.match(dest)
        if match:
            name, rest = match.groups()
            spec = self.config.destinations.get(name)
            if spec is None:
                known = ", ".join(sorted(self.config.destinations)) or "none configured"
                raise ConfigError(f"Unknown destination {name!r} (known: {known})")
            return self._resolve_alias(name, spec, rest)
        return self._local, dest

    def _resolve_alias(self, name: str, spec: dict, rest: str):
        rest = rest.replace("\\", "/").strip("/")
        if spec.get("url"):
            options = {**spec, **self.sftp_options}
            for key in ("password_file", "identity_file", "known_hosts"):
                if spec.get(key) and key not in self.sftp_options:
                    options[key] = self.config.resolve_relative(spec[key])
            settings, base = _settings_from_url(spec["url"], options)
            base = base.rstrip("/") or "/"
            remote_path = (base.rstrip("/") + "/" + rest) if rest else base
            mounted = self._mounted(settings, remote_path)
            if mounted:
                return mounted
            fs, base = self._open_sftp(spec["url"], options)
            return fs, fs.join(base, rest) if rest else base
        if spec.get("path"):
            base = os.path.expanduser(spec["path"])
            return self._local, os.path.join(base, *rest.split("/")) if rest else base
        raise ConfigError(f"Destination {name!r} needs either 'url' or 'path'")

    def _mounted(self, settings, remote_path: str):
        """``(fs, local path)`` when a configured mount covers this SFTP path and is
        reachable (and ``remote`` allows it); else None, or an error for ``off``."""
        if self.remote == "on":
            return None
        remote_path = "/" + remote_path.strip("/")
        for spec in self.config.destinations.values():
            if not spec.get("url") or not spec.get("mounts"):
                continue
            try:
                theirs, _ = _settings_from_url(spec["url"], {})
            except Exception:
                continue
            if (theirs.host, theirs.port, theirs.username) != (settings.host, settings.port, settings.username):
                continue
            for prefix, local in sorted(spec["mounts"].items(), key=lambda kv: -len(kv[0].strip("/"))):
                prefix = "/" + str(prefix).strip("/")
                if remote_path != prefix and not remote_path.startswith(prefix.rstrip("/") + "/"):
                    continue
                local_root = os.path.normpath(os.path.expanduser(str(local)))
                if not os.path.isdir(local_root):
                    break  # not mounted now; SFTP it is
                key = (settings.host, settings.port, settings.username, prefix)
                fs = self._mounts.get(key)
                if fs is None:
                    fs = MountFS(local_root, f"sftp://{settings.label}{prefix.rstrip('/')}")
                    self._mounts[key] = fs
                rest = remote_path[len(prefix):].strip("/")
                return fs, os.path.join(local_root, *rest.split("/")) if rest else local_root
        if self.remote == "off":
            raise ConfigError(f"--remote off, but no mount of sftp://{settings.label}{remote_path} is reachable.")
        return None

    def _open_sftp(self, url: str, options: dict):
        settings, path = _settings_from_url(url, options)
        key = (settings.host, settings.port, settings.username)
        fs = self._sftp.get(key)
        if fs is None:
            fs = SFTPFS(settings)
            self._sftp[key] = fs
        return fs, path.rstrip("/") or "/"

    def close(self) -> None:
        for fs in self._sftp.values():
            fs.close()
        self._sftp.clear()


def expand_output(template: str, provider: str) -> str:
    return template.replace("{provider}", provider)
