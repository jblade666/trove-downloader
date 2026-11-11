"""An SFTP destination written through its local mount when the mount is there."""

from __future__ import annotations

import os

import pytest

from trove.config import Config, Destinations
from trove.errors import ConfigError
from trove.fs import MountFS, SFTPFS


def destinations(tmp_path, remote="auto", mounted=True):
    drive = tmp_path / "drive"
    if mounted:
        drive.mkdir()
    config = Config(destinations={"nas": {"url": "sftp://me@nas.example:2222/", "mounts": {"/Books": str(drive)}}})
    return Destinations(config, remote=remote), drive


def test_an_alias_path_under_a_mount_is_written_locally(tmp_path):
    dests, drive = destinations(tmp_path)
    fs, path = dests.resolve("nas:/Books/shares/Temp")
    assert isinstance(fs, MountFS) and path == os.path.join(str(drive), "shares", "Temp")
    # The state file records the SFTP form, the same either way.
    assert fs.canonical(os.path.join(path, "2026-11")) == "sftp://me@nas.example:2222/Books/shares/Temp/2026-11"
    assert fs.canonical(str(drive)) == "sftp://me@nas.example:2222/Books"


def test_a_direct_sftp_url_on_the_same_server_uses_the_mount_too(tmp_path):
    dests, drive = destinations(tmp_path)
    fs, path = dests.resolve("sftp://me@nas.example:2222/Books/x")
    assert isinstance(fs, MountFS) and path == os.path.join(str(drive), "x")


def test_paths_outside_the_mount_and_other_servers_stay_sftp(tmp_path, monkeypatch):
    monkeypatch.setattr(SFTPFS, "__init__", lambda self, settings: setattr(self, "settings", settings))
    dests, _ = destinations(tmp_path)
    assert isinstance(dests.resolve("nas:/Music")[0], SFTPFS)
    assert isinstance(dests.resolve("nas:/Booksextra")[0], SFTPFS)
    assert isinstance(dests.resolve("sftp://me@other.example:2222/Books")[0], SFTPFS)


def test_an_unmounted_drive_falls_back_to_sftp(tmp_path, monkeypatch):
    monkeypatch.setattr(SFTPFS, "__init__", lambda self, settings: setattr(self, "settings", settings))
    dests, _ = destinations(tmp_path, mounted=False)
    assert isinstance(dests.resolve("nas:/Books/shares")[0], SFTPFS)


def test_remote_on_and_off(tmp_path, monkeypatch):
    monkeypatch.setattr(SFTPFS, "__init__", lambda self, settings: setattr(self, "settings", settings))
    on, _ = destinations(tmp_path, remote="on")
    assert isinstance(on.resolve("nas:/Books/shares")[0], SFTPFS)
    off, _ = destinations(tmp_path / "b", remote="off", mounted=False)
    with pytest.raises(ConfigError, match="--remote off"):
        off.resolve("nas:/Books/shares")
