from __future__ import annotations

import pytest

from conftest import FakeShare, make_listing
from trove import cli
from trove.errors import AbortRun, ShareError


def write_config(tmp_path, body):
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


class ListingShare(FakeShare):
    def __init__(self, contents):
        super().__init__(contents)

    def describe(self):
        return ["Fake share"]

    def list(self, subpath=""):
        return make_listing(self)


def run_main(argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    return exc.value.code


def test_no_destination_is_a_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "select_share", lambda *a: ListingShare({"a.txt": b"1"}))
    config = write_config(tmp_path, "{}")
    # A single failed share reports as a failure and names what's missing.
    assert run_main(["--config", config, "get", "https://x.example/s/a"]) == cli.EXIT_FAILED
    assert "No destination" in capsys.readouterr().out


def test_get_writes_into_the_share_name_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "select_share", lambda *a: ListingShare({"a.txt": b"hello"}))
    base = tmp_path / "out"
    base.mkdir()
    config = write_config(tmp_path, "{}")
    assert run_main(["--config", config, "get", "https://x.example/s/a", "-o", str(base)]) == cli.EXIT_OK
    assert (base / "Share" / "a.txt").read_bytes() == b"hello"


def test_missing_local_destination_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "select_share", lambda *a: ListingShare({"a.txt": b"hello"}))
    config = write_config(tmp_path, "{}")
    missing = tmp_path / "not-mounted"
    assert run_main(["--config", config, "get", "https://x.example/s/a", "-o", str(missing)]) == cli.EXIT_FAILED
    assert not missing.exists()
    assert "doesn't exist" in capsys.readouterr().out


def test_dest_with_path_lands_directly_in_dest(tmp_path, monkeypatch):
    share = ListingShare({"sub/a.txt": b"hello"})
    monkeypatch.setattr(cli, "select_share", lambda *a: share)
    dest = tmp_path / "exact"
    dest.mkdir()
    config = write_config(tmp_path, "{}")
    code = run_main(["--config", config, "get", "https://x.example/s/a", "--dest", str(dest), "-p", "sub"])
    assert code == cli.EXIT_OK and (dest / "a.txt").read_bytes() == b"hello"


def test_sync_continues_after_a_share_error_and_reports_it(tmp_path, monkeypatch, capsys):
    base = tmp_path / "out"
    base.mkdir()

    def select(urls, password, provider, *_):
        if "bad" in urls[0]:
            raise ShareError("dead link")
        return ListingShare({"a.txt": b"1"})

    monkeypatch.setattr(cli, "select_share", select)
    config = write_config(
        tmp_path,
        f"""
default_output: {str(base)!r}
sites:
  - {{name: Bad, url: "https://x.example/s/bad"}}
  - {{name: Good, url: "https://x.example/s/good", folder: "Group/Good"}}
""",
    )
    assert run_main(["--config", config, "sync"]) == cli.EXIT_FAILED
    assert (base / "Group" / "Good" / "a.txt").exists()
    out = capsys.readouterr().out
    assert "dead link" in out and "Good" in out.split("=== Summary ===")[1]


def test_sync_stops_on_abort(tmp_path, monkeypatch, capsys):
    calls = []

    def select(urls, password, provider, *_):
        calls.append(urls[0])
        raise AbortRun("quota exhausted")

    monkeypatch.setattr(cli, "select_share", select)
    config = write_config(
        tmp_path,
        """
sites:
  - {name: One, url: "https://x.example/s/1"}
  - {name: Two, url: "https://x.example/s/2"}
""",
    )
    assert run_main(["--config", config, "sync"]) == cli.EXIT_ABORTED
    out = capsys.readouterr().out
    assert len(calls) == 1 and "1 site(s) not attempted: the run stopped at One. quota exhausted" in out
    assert "STOPPED THE RUN" in out.split("=== Summary ===")[1]


def test_sync_summary_counts_each_site(tmp_path, monkeypatch, capsys):
    base = tmp_path / "out"
    base.mkdir()
    (base / "Two").mkdir()
    (base / "Two" / "old.txt").write_bytes(b"x")

    def select(urls, password, provider, *_):
        if "bad" in urls[0]:
            raise ShareError("dead link")
        return ListingShare({"a.txt": b"1", "b.txt": b"2"} if urls[0].endswith("1") else {"c.txt": b"3"})

    monkeypatch.setattr(cli, "select_share", select)
    config = write_config(
        tmp_path,
        f"""
default_output: {str(base)!r}
sites:
  - {{name: One, url: "https://x.example/s/1", folder: "One"}}
  - {{name: Two, url: "https://x.example/s/2", folder: "Two"}}
  - {{name: Bad, url: "https://x.example/s/bad"}}
""",
    )
    run_main(["--config", config, "sync", "--dry-run"])
    summary = capsys.readouterr().out.split("=== Summary ===")[1]
    assert "Would get" in summary
    rows = {line.split()[0]: line.split()[1:] for line in summary.splitlines()[2:5]}
    assert rows["One"] == ["ok", "2", "0", "0", "0"]
    assert rows["Two"] == ["ok", "1", "1", "0", "1"]  # the existing folder's date
    assert rows["Bad"] == ["failed"]
    assert "Total across 3 site(s): 3 to download, 1 date(s) to change, 0 error(s), 1 orphan(s)." in summary

    run_main(["--config", config, "sync"])
    summary = capsys.readouterr().out.split("=== Summary ===")[1]
    assert " New " in summary and "3 downloaded" in summary


def test_sync_filters_by_name(tmp_path, monkeypatch):
    seen = []

    def select(urls, password, provider, *_):
        seen.append(urls[0])
        raise ShareError("stop here")

    monkeypatch.setattr(cli, "select_share", select)
    config = write_config(
        tmp_path,
        """
sites:
  - {name: Alpha, url: "https://x.example/s/1"}
  - {name: Beta, url: "https://x.example/s/2"}
""",
    )
    run_main(["--config", config, "sync", "bet"])
    assert seen == ["https://x.example/s/2"]


def test_links_with_keys_are_not_printed(tmp_path, monkeypatch, capsys):
    def select(urls, password, provider, *_):
        raise ShareError("nope")

    monkeypatch.setattr(cli, "select_share", select)
    config = write_config(tmp_path, "{}")
    run_main(["--config", config, "get", "https://a.example/x#SECRET", "https://b.example/y#SECRET"])
    assert "SECRET" not in capsys.readouterr().out


def test_site_url_fragment_not_listed(tmp_path, capsys):
    config = write_config(tmp_path, 'sites:\n  - {name: One, url: "https://x.example/f#SECRET"}\n')
    assert run_main(["--config", config, "sites"]) == cli.EXIT_OK
    assert "SECRET" not in capsys.readouterr().out


def test_read_links_skips_comments(tmp_path):
    path = tmp_path / "links.txt"
    path.write_text("# note\nhttps://a\n\nhttps://b\n", encoding="utf-8")
    assert cli._read_links(str(path)) == ["https://a", "https://b"]


FOLDER_KEY = "A" * 22  # 16 bytes: a MEGA folder key
MEGA = f"https://mega.nz/folder/AbCdEfGh#{FOLDER_KEY}"


def _get_known_site(tmp_path, monkeypatch, link, sites, state=None, extra=()):
    seen = []

    def select(urls, password, provider, *_):
        seen.append((urls[0], provider))
        return ListingShare({"a.txt": b"hi"})

    monkeypatch.setattr(cli, "select_share", select)
    if state:
        (tmp_path / "state.json").write_text(state, encoding="utf-8")
    config = write_config(tmp_path, "sites:\n" + sites)
    code = run_main(["--config", config, "get", link, *extra])
    return code, seen


def test_get_on_a_known_sites_link_syncs_where_the_site_does(tmp_path, monkeypatch, capsys):
    out = tmp_path / "site-out"
    out.mkdir()
    sites = f'  - {{name: Known, url: "{MEGA}", output: "{out.as_posix()}", folder: Kept}}\n'
    code, seen = _get_known_site(tmp_path, monkeypatch, MEGA, sites)
    assert code == cli.EXIT_OK and (out / "Kept" / "a.txt").exists()
    assert "Known site: Known" in capsys.readouterr().out


def test_a_bare_mega_id_and_key_finds_its_site(tmp_path, monkeypatch):
    out = tmp_path / "site-out"
    out.mkdir()
    sites = f'  - {{name: Known, url: "{MEGA}", output: "{out.as_posix()}"}}\n'
    code, seen = _get_known_site(tmp_path, monkeypatch, f"AbCdEfGh#{FOLDER_KEY}", sites)
    assert code == cli.EXIT_OK and seen == [(MEGA, None)] and (out / "Share" / "a.txt").exists()


def test_a_site_behind_a_shortener_is_found_from_its_last_run(tmp_path, monkeypatch):
    out = tmp_path / "site-out"
    out.mkdir()
    sites = f'  - {{name: Short, url: "https://short.example/x", output: "{out.as_posix()}"}}\n'
    state = '{"sites": {"Short": {"identity": "mega:folder:AbCdEfGh"}}}'
    code, seen = _get_known_site(tmp_path, monkeypatch, MEGA, sites, state)
    assert code == cli.EXIT_OK and (out / "Share" / "a.txt").exists()


def test_the_command_line_wins_over_the_site(tmp_path, monkeypatch):
    mine = tmp_path / "mine"
    mine.mkdir()
    sites = f'  - {{name: Known, url: "{MEGA}", output: "{(tmp_path / "nope").as_posix()}"}}\n'
    code, _ = _get_known_site(tmp_path, monkeypatch, MEGA, sites, extra=["-o", str(mine)])
    assert code == cli.EXIT_OK and (mine / "Share" / "a.txt").exists()


def test_no_site_skips_the_lookup(tmp_path, monkeypatch, capsys):
    sites = f'  - {{name: Known, url: "{MEGA}", output: "{tmp_path.as_posix()}"}}\n'
    code, _ = _get_known_site(tmp_path, monkeypatch, MEGA, sites, extra=["--no-site"])
    assert code == cli.EXIT_FAILED and "No destination" in capsys.readouterr().out


def test_an_unknown_bare_id_needs_the_provider(tmp_path, monkeypatch, capsys):
    code, seen = _get_known_site(tmp_path, monkeypatch, "Ima81tmu", "  []\n".replace("  []", "  - {name: X, url: https://x.example/s/1}"))
    assert code == cli.EXIT_FAILED and not seen and "--provider" in capsys.readouterr().out


def test_a_bare_id_with_the_provider_becomes_its_link(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    code, seen = _get_known_site(tmp_path, monkeypatch, "Ima81tmu", "  []\n", extra=["--provider", "gofile", "-o", str(out)])
    assert code == cli.EXIT_OK and seen == [("Ima81tmu", "gofile")]


def test_set_password_mediafire_stores_the_login(tmp_path, monkeypatch, capsys):
    import keyring

    stored = {}
    monkeypatch.setattr(keyring, "set_password", lambda service, user, value: stored.__setitem__((service, user), value))
    monkeypatch.setattr("builtins.input", lambda prompt: "me@example.com")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "s3cret")
    config = write_config(tmp_path, "{}")
    assert run_main(["--config", config, "set-password", "mediafire"]) == cli.EXIT_OK
    assert stored == {("trove-mediafire", "email"): "me@example.com", ("trove-mediafire", "password"): "s3cret"}
    out = capsys.readouterr().out
    assert "s3cret" not in out and "me@example.com" not in out


def test_doctor_reports_no_mediafire_account(tmp_path, capsys):
    config = write_config(tmp_path, "{}")
    assert run_main(["--config", config, "doctor"]) == cli.EXIT_OK
    assert "MediaFire: no account" in capsys.readouterr().out
