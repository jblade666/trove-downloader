"""The run lock, --recent-first and --ages."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time

import pytest

from trove import cli
from trove.config import Site
from trove.lock import AlreadyRunning, RunLock
from trove.state import SiteState


def test_a_second_lock_in_the_same_process_is_refused_until_the_first_ends(tmp_path):
    path = str(tmp_path / "state.json.lock")
    with RunLock(path):
        with pytest.raises(AlreadyRunning):
            with RunLock(path):
                pass
    with RunLock(path):
        pass


def test_another_process_holding_the_lock_stops_a_sync(tmp_path):
    path = tmp_path / "state.json.lock"
    holder = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(__import__('pathlib').Path(cli.__file__).parents[1])!r})
        from trove.lock import RunLock
        with RunLock({str(path)!r}):
            print("held", flush=True)
            time.sleep(30)
    """)
    proc = subprocess.Popen([sys.executable, "-c", holder], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        with pytest.raises(AlreadyRunning, match="pid"):
            with RunLock(str(path)):
                pass
    finally:
        proc.kill()
        proc.wait()
    for _ in range(50):  # released when the holder dies; Windows takes a moment
        try:
            with RunLock(str(path)):
                break
        except AlreadyRunning:
            time.sleep(0.1)
    else:
        pytest.fail("the lock outlived the process holding it")


def test_sync_exits_with_its_own_code_when_another_is_running(tmp_path, capsys):
    (tmp_path / "config.yaml").write_text("{}", encoding="utf-8")
    (tmp_path / "sites.yaml").write_text('sites:\n  - {name: A, url: "https://x.example/s/a"}\n', encoding="utf-8")
    with RunLock(str(tmp_path / "state.json.lock")):
        with pytest.raises(SystemExit) as exc:
            cli.main(["--config", str(tmp_path / "config.yaml"), "sync"])
    assert exc.value.code == cli.EXIT_RUNNING
    assert "Another trove sync is running" in capsys.readouterr().out


def sites_with_state(tmp_path, newest: dict):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"sites": {n: {"newest_mtime": t, "files": 3} for n, t in newest.items() if t}}),
                    encoding="utf-8")
    sites = [Site(name=n, url=f"https://x.example/s/{n}") for n in newest]
    return sites, SiteState(str(path))


def test_recent_first_puts_unknown_then_newest_first(tmp_path):
    sites, state = sites_with_state(tmp_path, {"old": 1_000, "new": 3_000, "never": None, "mid": 2_000})
    assert [s.name for s in cli.recent_first(sites, state)] == ["never", "new", "mid", "old"]


def test_ages_lists_newest_first_and_marks_static(tmp_path, capsys):
    now = time.time()
    sites, state = sites_with_state(tmp_path, {"Old": now - 3 * 365 * 86400, "Fresh": now - 3 * 86400, "Ahead": now + 86400})
    cli.print_ages(sites, state, now)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[0] == "future" and "Ahead" in lines[0]
    assert lines[1].split()[0] == "3d" and "Fresh" in lines[1]
    assert "Old" in lines[2] and lines[2].endswith("static")
    assert "3 with a known date (1 static" in lines[-1]
