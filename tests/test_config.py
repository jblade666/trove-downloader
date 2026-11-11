from __future__ import annotations

import os

import pytest

from trove.config import Config, Destinations, expand_output, load_config
from trove.errors import ConfigError
from trove.fs import LocalFS, SFTPFS
from trove.naming import safe_component, safe_path


CONFIG = """
default_output: "nas:/files/{provider}"
keep_markers: ["(kept)"]
destinations:
  nas: {url: "sftp://someone@host.example:2222/base", keyring_service: "custom"}
  scratch: {path: "/tmp/scratch"}
sites:
  - name: One
    url: "https://cloud.example/s/abc"
    fallback: "https://cloud.example/s/def"
    password_file: "secrets/one.txt"
"""


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return load_config(str(path))


def test_load_config(config, tmp_path):
    assert config.default_output == "nas:/files/{provider}"
    assert config.keep_markers == ["(kept)"]
    site = config.sites[0]
    assert site.fallback == ["https://cloud.example/s/def"]
    assert config.resolve_relative(site.password_file) == os.path.join(str(tmp_path), "secrets/one.txt")


def test_explicit_config_must_exist(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "missing.yaml"))


def test_environment_config_must_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("TROVE_CONFIG", str(tmp_path / "missing.yaml"))
    with pytest.raises(ConfigError):
        load_config()


def test_alias_resolves_to_sftp(config):
    fs, path = Destinations(config).resolve("nas:/sub/dir")
    assert isinstance(fs, SFTPFS) and path == "/base/sub/dir"
    assert fs.settings.host == "host.example" and fs.settings.port == 2222
    assert fs.settings.username == "someone" and fs.settings.keyring_service == "custom"


def test_one_connection_per_server(config):
    destinations = Destinations(config)
    first, _ = destinations.resolve("nas:/a")
    second, _ = destinations.resolve("sftp://someone@host.example:2222/other")
    assert first is second


def test_sftp_url(config):
    fs, path = Destinations(config, {"identity_file": "key"}).resolve("sftp://me@h.example/x/y")
    assert isinstance(fs, SFTPFS) and path == "/x/y" and fs.settings.identity_file == "key"


def test_password_in_url_is_refused(config):
    with pytest.raises(ConfigError):
        Destinations(config).resolve("sftp://me:secret@h.example/x")


@pytest.mark.parametrize("path", ["R:\\files", "R:/files", "/mnt/files", "relative/dir"])
def test_plain_paths_are_local_and_untouched(config, path):
    fs, resolved = Destinations(config).resolve(path)
    assert isinstance(fs, LocalFS) and resolved == path


def test_unknown_alias_is_an_error(config):
    with pytest.raises(ConfigError):
        Destinations(config).resolve("nope:/x")


def test_alias_can_be_local(config):
    fs, path = Destinations(config).resolve("scratch:/a/b")
    assert isinstance(fs, LocalFS) and path == os.path.join("/tmp/scratch", "a", "b")


def test_expand_output():
    assert expand_output("nas:/files/{provider}", "allsync") == "nas:/files/allsync"


def test_empty_config_has_no_built_in_locations():
    assert Config().default_output is None and Config().destinations == {}


@pytest.mark.parametrize(
    "name,expected",
    [
        ("A: B", "A - B"),
        ("A:B", "A-B"),
        ('q?*"<>|\\', "q_______"),
        ("trailing. ", "trailing"),
        ("..", "_"),
        (".", "_"),
        ("NUL.txt", "_NUL.txt"),
        ("com1", "_com1"),
        ("fine.pdf", "fine.pdf"),
    ],
)
def test_safe_component(name, expected):
    assert safe_component(name) == expected


def test_safe_component_underscore_colon():
    assert safe_component("A: B", colon="underscore") == "A_ B"


def test_safe_path_cannot_escape():
    assert safe_path("../../a/./b") == "_/_/a/_/b"
