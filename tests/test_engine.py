from __future__ import annotations

import pytest

from conftest import FakeShare, make_listing
from trove.engine import SyncOptions, sync_listing
from trove.errors import ShareError

MTIME = 1_600_000_000.0


def write(fs, root, rel, data=b"x", mtime=MTIME):
    parts = rel.split("/")
    fs.makedirs(fs.join(root, *parts[:-1]) if parts[:-1] else root)
    path = fs.join(root, *parts)
    with fs.open(path, "wb") as fp:
        fp.write(data)
    fs.set_mtime(path, mtime)
    return path


def read(fs, root, rel):
    with fs.open(fs.join(root, *rel.split("/")), "rb") as fp:
        return fp.read()


def run(share, fs, root, listing=None, **options):
    listing = listing or make_listing(share)
    return sync_listing(listing, share, fs, root, SyncOptions(**options))


def test_downloads_new_files_and_sets_dates(dest):
    fs, root = dest
    share = FakeShare({"a.txt": b"hello", "sub/b.txt": b"world!"})
    result = run(share, fs, root)
    assert result.ok and result.downloaded == 2
    assert read(fs, root, "sub/b.txt") == b"world!"
    assert abs(fs.stat(fs.join(root, "a.txt")).mtime - MTIME) < 1
    assert abs(fs.stat(fs.join(root, "sub")).mtime - MTIME) < 1


def test_second_run_changes_nothing(dest):
    fs, root = dest
    share = FakeShare({"a.txt": b"hello", "sub/b.txt": b"world!"})
    run(share, fs, root)
    share.transfers.clear()
    result = run(share, fs, root)
    assert (result.downloaded, result.skipped, result.dates_changed) == (0, 2, 0)
    assert share.transfers == []


def test_dry_run_writes_nothing_and_never_opens_a_transfer(dest):
    fs, root = dest
    write(fs, root, "old.txt", b"old", mtime=5.0)
    share = FakeShare({"a.txt": b"hello", "old.txt": b"old", "sub/b.txt": b"x"})
    before = fs.stat(fs.join(root, "old.txt")).mtime
    result = run(share, fs, root, dry_run=True, delete_orphans=True)
    assert share.transfers == []
    assert result.would_download == 2 and result.downloaded == 0
    assert result.dates_changed >= 1
    assert fs.stat(fs.join(root, "a.txt")) is None
    assert fs.stat(fs.join(root, "sub")) is None
    assert fs.stat(fs.join(root, "old.txt")).mtime == before
    assert fs.stat(fs.join(root, share.manifest_name)) is None


def test_size_mismatch_redownloads(dest):
    fs, root = dest
    write(fs, root, "a.txt", b"short")
    share = FakeShare({"a.txt": b"much longer"})
    result = run(share, fs, root)
    assert result.downloaded == 1 and read(fs, root, "a.txt") == b"much longer"


def test_resume_uses_the_partial_file(dest):
    fs, root = dest
    share = FakeShare({"a.txt": b"0123456789"})
    write(fs, root, "a.txt.part", b"01234")
    result = run(share, fs, root)
    assert result.ok and share.transfers == [("a.txt", 5)]
    assert read(fs, root, "a.txt") == b"0123456789"


def test_server_ignoring_range_restarts_cleanly(dest):
    fs, root = dest
    share = FakeShare({"a.txt": b"0123456789"})
    share.ignore_range = True
    write(fs, root, "a.txt.part", b"01234")
    assert run(share, fs, root).ok
    assert read(fs, root, "a.txt") == b"0123456789"


def test_dropped_connection_retries_and_resumes(dest):
    fs, root = dest
    share = FakeShare({"a.txt": b"0123456789"})
    share.fail_after["a.txt"] = 4
    assert run(share, fs, root).ok
    assert [offset for _, offset in share.transfers][0] == 0
    assert read(fs, root, "a.txt") == b"0123456789"


def test_provider_without_resume_starts_over(dest):
    fs, root = dest
    share = FakeShare({"a.txt": b"0123456789"})
    share.can_resume = False
    write(fs, root, "a.txt.part", b"junk!")
    assert run(share, fs, root).ok
    assert share.transfers == [("a.txt", 0)]
    assert read(fs, root, "a.txt") == b"0123456789"


def test_permanent_failure_counts_and_moves_on(dest):
    fs, root = dest
    share = FakeShare({"a.txt": b"1", "b.txt": b"2"})
    share.missing.add("a.txt")
    result = run(share, fs, root)
    assert result.errors == 1 and result.downloaded == 1 and not result.ok
    assert len([t for t in share.transfers if t[0] == "a.txt"]) == 1


def test_hostile_names_stay_inside_the_output_folder(dest):
    fs, root = dest
    share = FakeShare({"../../evil.txt": b"x", "CON": b"y", "a: b?.txt": b"z"})
    assert run(share, fs, root).ok
    assert read(fs, root, "_/_/evil.txt") == b"x"
    assert read(fs, root, "_CON") == b"y"
    assert read(fs, root, "a - b_.txt") == b"z"


def test_verify_redownloads_only_when_the_tag_changed(dest):
    fs, root = dest
    share = FakeShare({"a.txt": b"AAAA", "b.txt": b"BBBB"})
    etags = {"a.txt": "1", "b.txt": "1"}
    run(share, fs, root, make_listing(share, etags=etags))
    share.contents["a.txt"] = b"CCCC"
    share.transfers.clear()
    listing = make_listing(share, etags={"a.txt": "2", "b.txt": "1"})
    result = run(share, fs, root, listing, verify=True)
    assert result.reverified == 1 and [t[0] for t in share.transfers] == ["a.txt"]
    assert read(fs, root, "a.txt") == b"CCCC"


def test_seed_manifest_baselines_without_downloading(dest):
    fs, root = dest
    write(fs, root, "a.txt", b"AAAA")
    share = FakeShare({"a.txt": b"AAAA"})
    run(share, fs, root, make_listing(share, etags={"a.txt": "1"}), seed_manifest=True)
    assert share.transfers == []
    share.contents["a.txt"] = b"BBBB"
    result = run(share, fs, root, make_listing(share, etags={"a.txt": "2"}), verify=True)
    assert result.reverified == 1


def test_orphans_are_reported_but_not_deleted_by_default(dest):
    fs, root = dest
    write(fs, root, "keep.txt", b"1")
    write(fs, root, "gone.txt", b"2")
    write(fs, root, "Thumbs.db", b"3")
    write(fs, root, "other.txt.part", b"4")
    result = run(FakeShare({"keep.txt": b"1"}), fs, root)
    assert result.orphans == ["gone.txt"]
    assert fs.stat(fs.join(root, "gone.txt")) is not None


@pytest.mark.parametrize("rel", ["old (kept)/a.txt", "dir/b (kept).txt"])
def test_keep_marker_matches_folders_and_files(dest, rel):
    fs, root = dest
    write(fs, root, "keep.txt", b"1")
    write(fs, root, rel, b"2")
    result = run(FakeShare({"keep.txt": b"1"}), fs, root, keep_markers=("(kept)",))
    assert result.orphans == []


def test_delete_orphans_removes_files_and_empty_folders(dest):
    fs, root = dest
    for i in range(9):
        write(fs, root, f"keep{i}.txt", b"1")
    write(fs, root, "old/deep/gone.txt", b"2")
    share = FakeShare({f"keep{i}.txt": b"1" for i in range(9)})
    result = run(share, fs, root, delete_orphans=True)
    assert result.deleted == 1 and result.ok
    assert fs.stat(fs.join(root, "old")) is None


def test_delete_refused_when_listing_has_no_files(dest):
    fs, root = dest
    write(fs, root, "a.txt", b"1")
    share = FakeShare({})
    listing = make_listing(share, folders=["emptied"])
    result = run(share, fs, root, listing, delete_orphans=True)
    assert not result.ok and result.deleted == 0
    assert fs.stat(fs.join(root, "a.txt")) is not None


def test_delete_refused_over_the_percentage_limit(dest):
    fs, root = dest
    for i in range(10):
        write(fs, root, f"f{i}.txt", b"1")
    share = FakeShare({"f0.txt": b"1", "f1.txt": b"1"})
    result = run(share, fs, root, delete_orphans=True)
    assert not result.ok and result.deleted == 0
    assert fs.stat(fs.join(root, "f9.txt")) is not None
    result = run(share, fs, root, delete_orphans=True, delete_max_pct=100)
    assert result.deleted == 8


def test_delete_refused_for_a_partial_run(dest):
    fs, root = dest
    write(fs, root, "a.txt", b"1")
    write(fs, root, "b.txt", b"2")
    share = FakeShare({"a.txt": b"1"})
    result = run(share, fs, root, delete_orphans=True, only=("a*",), delete_max_pct=100)
    assert not result.ok and result.deleted == 0


def test_only_narrows_fetching_but_not_the_orphan_check(dest):
    fs, root = dest
    write(fs, root, "b/unselected.txt", b"12")
    share = FakeShare({"a/wanted.pdf": b"1", "b/unselected.txt": b"12"})
    result = run(share, fs, root, only=("*.PDF",))
    assert result.downloaded == 1 and result.orphans == []
    assert [t[0] for t in share.transfers] == ["a/wanted.pdf"]


def test_only_with_no_match_is_an_error(dest):
    fs, root = dest
    with pytest.raises(ShareError):
        run(FakeShare({"a.txt": b"1"}), fs, root, only=("*.pdf",))


def test_subpath_writes_where_a_full_sync_would(dest):
    fs, root = dest
    write(fs, root, "other/stays.txt", b"1")
    share = FakeShare({"sub/a.txt": b"hello"})
    listing = make_listing(share)
    listing.folders = []
    result = run(share, fs, root, listing, subpath="sub")
    assert read(fs, root, "sub/a.txt") == b"hello"
    assert result.orphans == []  # the scan stays inside sub/


def test_subpath_with_reroot_lands_directly_in_the_output_folder(dest):
    fs, root = dest
    share = FakeShare({"a/b/c.txt": b"hello"})
    result = run(share, fs, root, subpath="a/b", reroot=True)
    assert result.ok and read(fs, root, "c.txt") == b"hello"


def test_rollover_moves_instead_of_redownloading(dest):
    fs, root = dest
    write(fs, root, "2026-01/a.txt", b"hello")
    write(fs, root, share_manifest := ".fake-manifest.json", b'{"2026-01/a.txt": "1"}')
    share = FakeShare({"Archive/2026-01/a.txt": b"hello", "2026-02/b.txt": b"new"})
    result = run(share, fs, root, make_listing(share, etags={"Archive/2026-01/a.txt": "1"}), rollover="Archive")
    assert [t[0] for t in share.transfers] == ["2026-02/b.txt"]
    assert read(fs, root, "Archive/2026-01/a.txt") == b"hello"
    assert b"Archive/2026-01/a.txt" in read(fs, root, share_manifest)
    assert result.orphans == []


def test_dates_only_touches_only_dates(dest):
    fs, root = dest
    write(fs, root, "a.txt", b"1", mtime=5.0)
    share = FakeShare({"a.txt": b"1", "missing.txt": b"2"})
    result = run(share, fs, root, dates_only=True)
    assert share.transfers == []
    assert abs(fs.stat(fs.join(root, "a.txt")).mtime - MTIME) < 1
    assert fs.stat(fs.join(root, "missing.txt")) is None
    assert result.dates_changed >= 1


def test_each_directory_is_listed_once(dest):
    fs, root = dest
    if not fs.is_remote:
        pytest.skip("only counts round trips on the SFTP-like tree")
    share = FakeShare({f"d/f{i}.txt": b"1" for i in range(50)})
    for i in range(50):
        write(fs, root, f"d/f{i}.txt", b"1")
    fs.calls = 0
    listing = make_listing(share)
    run(share, fs, root, listing, no_orphans=True)
    assert fs.calls < 12


def test_case_twins_match_existing_copies_by_size(dest):
    fs, root = dest
    write(fs, root, "d/Book.pdf", b"L" * 70)
    write(fs, root, "d/BOOK (1).pdf", b"S" * 17)
    share = FakeShare({"d/BOOK.pdf": b"S" * 17, "d/Book.pdf": b"L" * 70})
    result = run(share, fs, root)
    assert share.transfers == []
    assert result.skipped == 2 and result.downloaded == 0 and result.orphans == []
    assert read(fs, root, "d/Book.pdf") == b"L" * 70
    assert read(fs, root, "d/BOOK (1).pdf") == b"S" * 17


def test_case_twins_download_to_separate_names_and_then_stay_put(dest):
    fs, root = dest
    share = FakeShare({"BOOK.pdf": b"S" * 17, "Book.pdf": b"L" * 70})
    result = run(share, fs, root)
    assert result.ok and result.downloaded == 2
    names = {n.lower(): st.size for n, st in fs.list_files(root).items() if n.lower().endswith(".pdf")}
    assert sorted(names.values()) == [17, 70] and len(names) == 2
    share.transfers.clear()
    result = run(share, fs, root)
    assert share.transfers == [] and result.skipped == 2 and result.orphans == [] and result.renamed == 0


def test_case_twin_never_overwrites_a_file_it_doesnt_match(dest):
    fs, root = dest
    write(fs, root, "Book.pdf", b"X" * 99)
    share = FakeShare({"BOOK.pdf": b"S" * 17, "Book.pdf": b"L" * 70})
    result = run(share, fs, root, dry_run=True)
    assert result.would_download == 2
    assert read(fs, root, "Book.pdf") == b"X" * 99
    run(share, fs, root)
    assert read(fs, root, "Book.pdf") == b"X" * 99
    sizes = sorted(st.size for n, st in fs.list_files(root).items() if n.endswith(".pdf"))
    assert sizes == [17, 70, 99]


def test_remote_case_rename_is_a_skip_and_gets_renamed():
    from conftest import MemFS

    fs = MemFS()
    fs.makedirs("/out")
    write(fs, "/out", "Tomb Of Doom.pdf", b"123")
    share = FakeShare({"Tomb of Doom.pdf": b"123"})
    result = run(share, fs, "/out")
    assert result.skipped == 1 and result.downloaded == 0
    assert result.renamed == 1 and result.orphans == []
    assert "/out/Tomb of Doom.pdf" in fs.files


def test_a_refused_date_is_a_failure_and_never_counted_as_done(dest, capsys):
    fs, root = dest
    share = FakeShare({"a.txt": b"data", "sub/b.txt": b"data"})
    sync_listing(make_listing(share), share, fs, root, SyncOptions())

    def refuse(path, mtime):
        raise PermissionError(13, "Permission denied")

    fs.set_mtime = refuse
    capsys.readouterr()
    result = sync_listing(make_listing(share, mtime=1_500_000_000.0), share, fs, root, SyncOptions(dates_only=True))
    out = capsys.readouterr().out
    assert result.dates_changed == 0
    assert result.date_failures == result.errors == 4  # two files, one folder, the root
    assert not result.ok
    assert "0 updated" in out and "could not be set" in out


def test_a_refused_date_on_a_fresh_download_keeps_the_file(dest):
    fs, root = dest

    def refuse(path, mtime):
        raise PermissionError(13, "Permission denied")

    fs.set_mtime = refuse
    share = FakeShare({"a.txt": b"data"})
    result = sync_listing(make_listing(share), share, fs, root, SyncOptions())
    assert result.downloaded == 1 and result.dates_changed == 0 and result.date_failures >= 1
    with fs.open(fs.join(root, "a.txt"), "rb") as fp:
        assert fp.read() == b"data"


def test_no_name_ever_holds_a_slash():
    from trove.naming import safe_component

    for style in ("dash", "underscore", "percent"):
        assert "/" not in safe_component("a/../b", style)


def test_walk_stats_does_not_follow_symlinked_folders(tmp_path):
    import os

    from trove.fs import LocalFS

    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f.txt").write_text("x")
    (tmp_path / "tree").mkdir()
    try:
        os.symlink(tmp_path / "real", tmp_path / "tree" / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges here")
    walked = [d for d, _dirs, _files in LocalFS().walk_stats(str(tmp_path / "tree"))]
    assert walked == [str(tmp_path / "tree")]
