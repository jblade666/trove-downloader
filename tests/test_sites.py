from __future__ import annotations

import json

import pytest
import requests

from conftest import FakeShare, make_listing
from trove import cli
from trove.config import load_config
from trove.errors import ConfigError, ShareError
from trove.providers import allsync, is_gone_error, select_share
from trove.state import ShareInfo, SiteState, describe_site


class ListingShare(FakeShare):
    def list(self, subpath=""):
        return make_listing(self)


def setup_files(tmp_path, sites, config="{}"):
    (tmp_path / "config.yaml").write_text(config, encoding="utf-8")
    (tmp_path / "sites.yaml").write_text(sites, encoding="utf-8")
    return str(tmp_path / "config.yaml")


def run_main(argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    return exc.value.code


def info(identity="p:1", provider="allsync", name="Share"):
    return ShareInfo(identity, provider, name, 3, 1_600_000_000.0)


def test_sites_file_is_read_from_beside_the_config(tmp_path):
    path = setup_files(tmp_path, 'sites:\n  - {name: One, url: "https://x.example/s/1", enabled: false, note: "why"}\n')
    site = load_config(path).sites[0]
    assert site.name == "One" and site.enabled is False and site.note == "why"


def test_named_sites_file_must_exist(tmp_path):
    (tmp_path / "config.yaml").write_text("sites_file: other.yaml\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "config.yaml"))


def test_duplicate_site_names_are_refused(tmp_path):
    path = setup_files(
        tmp_path,
        'sites:\n  - {name: One, url: "https://x.example/s/1"}\n  - {name: one, url: "https://x.example/s/2"}\n',
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_bad_entry_error_doesnt_reveal_link_keys(tmp_path):
    path = setup_files(tmp_path, 'sites:\n  - {url: "https://x.example/f#SECRET"}\n')
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "SECRET" not in str(exc.value)


def test_disabled_sites_are_left_out_unless_named_in_full(tmp_path, monkeypatch, capsys):
    seen = []

    def select(urls, password, provider, *_):
        seen.append(urls[0])
        raise ShareError("stop")

    monkeypatch.setattr(cli, "select_share", select)
    path = setup_files(
        tmp_path,
        """
sites:
  - {name: Live, url: "https://x.example/s/live"}
  - {name: Retired Site, url: "https://x.example/s/old", enabled: false}
""",
    )
    run_main(["--config", path, "sync"])
    assert seen == ["https://x.example/s/live"]
    assert "1 disabled site(s) left out" in capsys.readouterr().out

    seen.clear()
    run_main(["--config", path, "sync", "retired"])
    assert seen == []
    run_main(["--config", path, "sync", "retired site"])
    assert seen == ["https://x.example/s/old"]


def test_state_tracks_failure_and_recovery(tmp_path, monkeypatch, capsys):
    base = tmp_path / "out"
    base.mkdir()
    outcome = {"fail": True}

    def select(urls, password, provider, *_):
        if outcome["fail"]:
            raise ShareError("No usable link.", gone=True)
        return ListingShare({"a.txt": b"1"})

    monkeypatch.setattr(cli, "select_share", select)
    path = setup_files(tmp_path, 'sites:\n  - {name: One, url: "https://x.example/s/1"}\n', f"default_output: {str(base)!r}\n")

    assert run_main(["--config", path, "sync"]) == cli.EXIT_FAILED
    assert "LINK GONE" in capsys.readouterr().out
    run_main(["--config", path, "sync"])
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["sites"]["One"]
    assert saved["failures"] == 2 and saved["gone"] is True

    capsys.readouterr()
    assert run_main(["--config", path, "sites", "--gone", "--brief"]) == cli.EXIT_OK
    assert "GONE since" in capsys.readouterr().out

    outcome["fail"] = False
    assert run_main(["--config", path, "sync"]) == cli.EXIT_OK
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["sites"]["One"]
    assert "failures" not in saved and saved["files"] == 1
    capsys.readouterr()
    run_main(["--config", path, "sites", "--gone", "--brief"])
    assert "One" not in capsys.readouterr().out


def test_config_problem_isnt_recorded_against_the_site(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "select_share", lambda *a: ListingShare({"a.txt": b"1"}))
    path = setup_files(tmp_path, 'sites:\n  - {name: One, url: "https://x.example/s/1"}\n')
    assert run_main(["--config", path, "sync"]) == cli.EXIT_FAILED  # no destination set
    assert not (tmp_path / "state.json").exists() or "One" not in json.loads(
        (tmp_path / "state.json").read_text(encoding="utf-8")
    )["sites"]


def test_state_warns_when_a_link_moves(tmp_path):
    state = SiteState(str(tmp_path / "s.json"))
    assert state.record_success("One", info()) == []
    assert state.record_success("One", info()) == []
    assert "different share" in state.record_success("One", info(identity="p:2"))[0]
    assert "moved from allsync to koofr" in state.record_success("One", info("k:1", "koofr"))[0]
    assert "renamed" in state.record_success("One", info("k:1", "koofr", "New Name"))[0]


def test_state_survives_a_save_and_a_broken_file(tmp_path, capsys):
    path = tmp_path / "s.json"
    state = SiteState(str(path))
    state.record_failure("One", "boom", gone=False)
    state.save()
    assert SiteState(str(path)).get("One")["last_error"] == "boom"
    path.write_text("{broken", encoding="utf-8")
    assert SiteState(str(path)).sites == {}
    assert "ignoring" in capsys.readouterr().out


def test_describe_site():
    assert describe_site({}) == "not run yet"
    assert "failing since" in describe_site({"failures": 1, "failing_since": 1.0})
    assert "GONE since" in describe_site({"failures": 2, "gone": True, "failing_since": 1.0, "last_ok": 2.0})


def http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.exceptions.HTTPError(response=response)


@pytest.mark.parametrize(
    "error,expected",
    [
        (http_error(404), True),
        (http_error(410), True),
        (http_error(500), False),
        (http_error(401), False),
        (ValueError("no redirect"), True),
        (ConnectionError("down"), False),
        (ShareError("x", gone=True), True),
        (ShareError("x"), False),
    ],
)
def test_is_gone_error(error, expected):
    assert is_gone_error(error) is expected


def test_select_share_reports_gone_only_if_every_link_is_dead(monkeypatch):
    def probe(self):
        raise http_error(500 if self.token == "busy" else 404)

    monkeypatch.setattr(allsync.AllsyncShare, "probe", probe)
    monkeypatch.setattr("trove.providers.is_provider_url", lambda u: "/s/" in u)

    def dead_shortener(url, is_final):
        raise ValueError("no redirect")

    monkeypatch.setattr("trove.providers.resolve_shortener", dead_shortener)
    with pytest.raises(ShareError) as exc:
        select_share(["https://short.example/a", "https://short.example/b"])
    assert exc.value.gone is True

    def pass_through(url, is_final):
        return url

    monkeypatch.setattr("trove.providers.resolve_shortener", pass_through)
    # The last link isn't probed, so a dead first link falls through to it.
    share = select_share(["https://c.example/s/dead", "https://c.example/s/last"])
    assert share.token == "last"


def test_sites_sharing_a_folder_share_the_orphan_check(tmp_path, monkeypatch, capsys):
    """Two remote folders with the same name land in one local folder. Each one's
    files must not be the other's orphans, and certainly not deleted as such."""
    out = tmp_path / "out"
    out.mkdir()
    contents = {"https://x.example/s/a": {"a.pdf": b"aaa"}, "https://x.example/s/b": {"b.pdf": b"bbbb"}}

    class Named(ListingShare):
        def __init__(self, url):
            super().__init__(dict(contents[url]))
            self.url = url

        @property
        def identity(self):
            return self.url

    monkeypatch.setattr(cli, "select_share", lambda urls, password, provider, *_: Named(urls[0]))
    path = setup_files(
        tmp_path,
        f"""
sites:
  - {{name: A, url: "https://x.example/s/a", output: "{out.as_posix()}"}}
  - {{name: B, url: "https://x.example/s/b", output: "{out.as_posix()}"}}
""",
    )
    run_main(["--config", path, "sync"])
    first = capsys.readouterr().out
    assert "next sync checks their orphans together" in first

    assert run_main(["--config", path, "sync", "--delete-orphans"]) == cli.EXIT_OK
    second = capsys.readouterr().out
    assert "=== Orphans in" in second and "ORPHAN:" not in second
    assert (out / "Share" / "a.pdf").read_bytes() == b"aaa" and (out / "Share" / "b.pdf").read_bytes() == b"bbbb"


def test_a_shared_folder_deletes_nothing_when_a_site_fails(tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    (out / "Share").mkdir(parents=True)
    (out / "Share" / "b.pdf").write_bytes(b"bbbb")

    def select(urls, password, provider, *_):
        if urls[0].endswith("/b"):
            raise ShareError("down")
        return ListingShare({"a.pdf": b"aaa"})

    monkeypatch.setattr(cli, "select_share", select)
    path = setup_files(
        tmp_path,
        f"""
sites:
  - {{name: A, url: "https://x.example/s/a", output: "{out.as_posix()}"}}
  - {{name: B, url: "https://x.example/s/b", output: "{out.as_posix()}"}}
""",
    )
    from trove.fs import LocalFS

    fs = LocalFS()
    shared = fs.describe(fs.join(out.as_posix(), "Share"))
    state = {"sites": {n: {"output": shared, "identity": n, "provider": "allsync"} for n in ("A", "B")}}
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    run_main(["--config", path, "sync", "--delete-orphans"])
    assert "nothing is deleted" in capsys.readouterr().out
    assert (out / "Share" / "b.pdf").exists()


def test_a_first_run_with_delete_orphans_never_deletes_a_co_sites_files(tmp_path, monkeypatch, capsys):
    """Nothing recorded yet says the two sites share a folder, so the first one to
    list mustn't delete the second one's files as orphans."""
    out = tmp_path / "out"
    (out / "Share").mkdir(parents=True)
    (out / "Share" / "b.pdf").write_bytes(b"bbbb")
    contents = {"https://x.example/s/a": {"a.pdf": b"aaa"}, "https://x.example/s/b": {"b.pdf": b"bbbb"}}
    monkeypatch.setattr(cli, "select_share", lambda urls, password, provider, *_: ListingShare(dict(contents[urls[0]])))
    path = setup_files(
        tmp_path,
        f"""
sites:
  - {{name: A, url: "https://x.example/s/a", output: "{out.as_posix()}"}}
  - {{name: B, url: "https://x.example/s/b", output: "{out.as_posix()}"}}
""",
    )
    run_main(["--config", path, "sync", "--delete-orphans", "--delete-max-pct", "100"])
    assert (out / "Share" / "b.pdf").read_bytes() == b"bbbb"
    assert "DELETED" not in capsys.readouterr().out


def test_sync_selects_sites_by_provider(tmp_path, monkeypatch, capsys):
    seen = []

    def select(urls, password, provider, *_):
        seen.append(urls[0])
        raise ShareError("stop here")

    monkeypatch.setattr(cli, "select_share", select)
    path = setup_files(
        tmp_path,
        """
sites:
  - {name: Room, url: "https://k.example/r/room"}
  - {name: Token, url: "https://x.example/s/token"}
  - {name: Forced, url: "https://x.example/s/forced", provider: Kregfile}
  - {name: Short, url: "https://short.example/abc"}
  - {name: New, url: "https://short.example/new"}
""",
    )
    state = {"sites": {"Short": {"provider": "kregfile"}}}
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    run_main(["--config", path, "sync", "--provider", "kregfile"])
    assert seen == ["https://k.example/r/room", "https://x.example/s/forced", "https://short.example/abc"]
    assert "1 site(s) left out" in capsys.readouterr().out

    seen.clear()
    run_main(["--config", path, "sync", "--provider", "allsync", "--provider", "kregfile", "o"])
    assert seen == ["https://k.example/r/room", "https://x.example/s/token", "https://x.example/s/forced",
                    "https://short.example/abc"]


def test_an_unknown_provider_is_refused(tmp_path, capsys):
    path = setup_files(tmp_path, 'sites:\n  - {name: One, url: "https://x.example/s/1"}\n')
    assert run_main(["--config", path, "sync", "--provider", "nosuch"]) != cli.EXIT_OK
    assert "Unknown provider nosuch" in capsys.readouterr().err


def test_a_folder_shared_with_a_site_left_out_is_not_judged(tmp_path, monkeypatch, capsys):
    """Syncing one of two sites that share a folder mustn't take the other's files
    for orphans, let alone delete them."""
    out = tmp_path / "out"
    (out / "Share").mkdir(parents=True)
    (out / "Share" / "b.pdf").write_bytes(b"bbbb")
    monkeypatch.setattr(cli, "select_share", lambda *a: ListingShare({"a.pdf": b"aaa"}))
    path = setup_files(
        tmp_path,
        f"""
sites:
  - {{name: Alpha, url: "https://x.example/s/a", output: "{out.as_posix()}"}}
  - {{name: Beta, url: "https://x.example/s/b", output: "{out.as_posix()}"}}
""",
    )
    from trove.fs import LocalFS

    fs = LocalFS()
    shared = fs.describe(fs.join(out.as_posix(), "Share"))
    state = {"sites": {n: {"output": shared, "identity": n, "provider": "allsync"} for n in ("Alpha", "Beta")}}
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    assert run_main(["--config", path, "sync", "alpha", "--delete-orphans", "--delete-max-pct", "100"]) == cli.EXIT_OK
    printed = capsys.readouterr().out
    assert "Not checked: Beta" in printed and "ORPHAN:" not in printed
    assert (out / "Share" / "b.pdf").read_bytes() == b"bbbb"
    assert (out / "Share" / "a.pdf").read_bytes() == b"aaa"
