"""The helper interface: --json reports and trove.api."""

from __future__ import annotations

import json

import pytest

from conftest import FakeShare, make_listing
from trove import api, cli
from trove.errors import AbortRun, ConfigError, ShareError
from trove.lock import RunLock


class ListingShare(FakeShare):
    def __init__(self, contents, url):
        super().__init__(contents)
        self.url = url

    @property
    def identity(self):
        return self.url

    def list(self, subpath=""):
        listing = make_listing(self)
        listing.share_name = self.url.rsplit("/", 1)[1]
        return listing


@pytest.fixture
def setup(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    (tmp_path / "config.yaml").write_text("{}", encoding="utf-8")
    behaviour = {}

    def select(urls, password, provider, *_):
        what = behaviour.get(urls[0].split("#")[0])
        if isinstance(what, Exception):
            raise what
        return ListingShare({"a.pdf": b"aaa", "b.pdf": b"bb"}, urls[0])

    monkeypatch.setattr(cli, "select_share", select)
    return tmp_path, out, behaviour


def test_api_get_reports_each_link(setup):
    tmp_path, out, behaviour = setup
    behaviour["https://x.example/s/dead"] = ShareError("gone", gone=True)
    report = api.get(["https://x.example/s/one", "https://x.example/s/dead#KEY"], output=str(out),
                     config=str(tmp_path / "config.yaml"), quiet=True)
    assert report.exit_code == cli.EXIT_FAILED and not report.aborted
    one, dead = report.shares
    assert (one.status, one.downloaded, one.share_name) == ("ok", 2, "one")
    assert (dead.status, dead.link) == ("gone", "https://x.example/s/dead")  # no #fragment
    assert (out / "one" / "a.pdf").read_bytes() == b"aaa"


def test_a_stopped_run_marks_the_rest_not_run(setup):
    tmp_path, out, behaviour = setup
    behaviour["https://x.example/s/b"] = AbortRun("quota")
    report = api.get(["https://x.example/s/a", "https://x.example/s/b", "https://x.example/s/c"],
                     output=str(out), config=str(tmp_path / "config.yaml"), quiet=True)
    assert [s.status for s in report.shares] == ["ok", "aborted", "not_run"]
    assert report.aborted and report.exit_code == cli.EXIT_ABORTED


def test_json_on_stdout_is_only_json(setup, capsys):
    tmp_path, out, _behaviour = setup
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config", str(tmp_path / "config.yaml"), "get", "https://x.example/s/one",
                  "-o", str(out), "--dry-run", "--json", "-"])
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert exc.value.code == 0 and data["version"] == 1 and data["dry_run"] is True
    assert data["shares"][0]["would_download"] == 2 and data["shares"][0]["status"] == "ok"
    assert "Done:" in captured.err


def test_json_to_a_file_and_a_config_error_still_gives_a_report(setup):
    tmp_path, out, _behaviour = setup
    report_path = tmp_path / "report.json"
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config", str(tmp_path / "config.yaml"), "get", "--json", str(report_path)])
    assert exc.value.code == cli.EXIT_USAGE
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["exit_code"] == cli.EXIT_USAGE and "No links" in data["error"]


def test_api_raises_config_errors(setup):
    tmp_path, _out, _behaviour = setup
    with pytest.raises(ConfigError):
        api.get([], config=str(tmp_path / "config.yaml"), quiet=True)


def test_api_sync_reports_another_run(setup):
    tmp_path, out, _behaviour = setup
    (tmp_path / "sites.yaml").write_text(f'sites:\n  - {{name: A, url: "https://x.example/s/a", output: "{out.as_posix()}"}}\n',
                                         encoding="utf-8")
    with RunLock(str(tmp_path / "state.json.lock")):
        report = api.sync(config=str(tmp_path / "config.yaml"), quiet=True)
    assert report.exit_code == cli.EXIT_RUNNING and report.shares == [] and "running" in report.error
    report = api.sync(config=str(tmp_path / "config.yaml"), quiet=True)
    assert [(s.site, s.status, s.downloaded) for s in report.shares] == [("A", "ok", 2)]


def test_pause_waits_between_links_only(setup, monkeypatch):
    tmp_path, out, _behaviour = setup
    waits = []
    monkeypatch.setattr(cli.time, "sleep", waits.append)
    api.get(["https://x.example/s/a", "https://x.example/s/b"], output=str(out), pause=2.5,
            config=str(tmp_path / "config.yaml"), quiet=True)
    assert waits == [2.5]


def test_recognizes_supported_links_only():
    assert api.recognizes("https://gofile.io/d/AbC123")
    assert api.recognizes("https://mega.nz/folder/AbCdEf12#" + "k" * 22)
    assert not api.recognizes("https://short.example/abc")
    assert not api.recognizes("https://drivethrurpg.example/product/564646")


def test_config_error_is_part_of_the_api():
    from trove.errors import ConfigError

    assert api.ConfigError is ConfigError
    assert "ConfigError" in api.__all__


MEGA_LINK = "https://mega.nz/folder/AbCdEfGh#" + "A" * 22


def test_identify_names_a_share_without_going_online():
    direct = api.identify(MEGA_LINK)
    assert direct.provider == "mega" and direct.identity and direct.error is None
    assert api.identify(MEGA_LINK.replace("folder/", "#F!").replace("#A", "!A")).identity == direct.identity


def test_identify_follows_a_shortener_by_hand(monkeypatch):
    monkeypatch.setattr("trove.http.resolve_shortener", lambda url, is_final, **k: MEGA_LINK)
    info = api.identify("https://short.example/abc")
    assert (info.final, info.provider) == (MEGA_LINK, "mega")
    assert api.identify("https://short.example/abc", follow=False).error


def test_identify_reports_a_dead_shortener(monkeypatch):
    def dead(url, is_final, **k):
        raise ValueError("short.example answered HTTP 404 instead of redirecting")

    monkeypatch.setattr("trove.http.resolve_shortener", dead)
    info = api.identify("https://short.example/gone")
    assert info.identity is None and "404" in info.error


def test_sites_lists_the_config_with_what_runs_recorded(tmp_path):
    (tmp_path / "config.yaml").write_text("{}", encoding="utf-8")
    (tmp_path / "sites.yaml").write_text(
        "sites:\n"
        '  - {name: One, url: "https://x.example/s/one", fallback: "https://y.example/s/one"}\n'
        '  - {name: Short, url: "https://short.example/two", enabled: false, link_in_document: [k.example]}\n',
        encoding="utf-8",
    )
    state = {"sites": {"Short": {"provider": "mega", "identity": "mega:folder:X", "output": "/out/Two"}}}
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    one, short = api.sites(config=str(tmp_path / "config.yaml"))
    assert (one.name, one.fallback, one.provider, one.identity) == ("One", ("https://y.example/s/one",), "allsync", None)
    assert (short.enabled, short.provider, short.identity, short.output) == (False, "mega", "mega:folder:X", "/out/Two")
    assert (one.link_in_document, short.link_in_document) == ((), ("k.example",))


def test_open_folder_reads_a_destination(tmp_path):
    (tmp_path / "config.yaml").write_text("{}", encoding="utf-8")
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "b.pdf").write_bytes(b"bb")
    (folder / "a.pdf").write_bytes(b"a")
    (folder / "sub").mkdir()
    with api.open_folder(str(folder), config=str(tmp_path / "config.yaml")) as docs:
        assert [(f.name, f.size) for f in docs.files()] == [("a.pdf", 1), ("b.pdf", 2)]
        assert docs.read("b.pdf") == b"bb"
