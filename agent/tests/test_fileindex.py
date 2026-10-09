"""The file index on a temporary tree: budgets, exclusions, depth, links, placeholders,
FTS and LIKE search, pauses from the governor, and the live fallback.

Nothing here touches the user's folders. ``default_roots`` is tested with the home
folder and the known-folder lookup replaced.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from coworker.fileindex import Budgets, FileIndex, FileRecord, default_roots
from coworker.fileindex import crawl as crawl_mod
from coworker.fileindex.index import USER_FOLDERS
from coworker.governor.model import GovTimeout, Refused

CLOCK = 1_700_000_000.0  # one fixed instant: every write lands in the same day and hour


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _names(index: FileIndex) -> list[str]:
    with index._lock:
        return [r["name"] for r in index._conn.execute("SELECT name FROM files ORDER BY name")]


def _row(index: FileIndex, name: str):
    with index._lock:
        return index._conn.execute("SELECT * FROM files WHERE name = ?", (name,)).fetchone()


class FakeGovernor:
    """Synchronous governor. ``refusals`` lists the codes to refuse, one per call, in order."""

    def __init__(self, refusals=(), timeouts_on: set[int] = frozenset()):
        self.refusals = list(refusals)
        self.timeouts_on = set(timeouts_on)
        self.calls: list[str] = []

    def run(self, gov_class, fn, timeout_s, cancel=None):
        self.calls.append(gov_class)
        if self.refusals:
            raise Refused(self.refusals.pop(0))
        if timeout_s in self.timeouts_on:
            raise GovTimeout()
        return fn()


class AsyncGovernor:
    """Shaped like the real governor: ``run`` is a coroutine."""

    def __init__(self):
        self.calls = 0

    async def run(self, gov_class, fn, *, timeout_s, cancel=None, interactive=True):
        self.calls += 1
        return fn()


@pytest.fixture
def root(tmp_path) -> Path:
    folder = tmp_path / "root"
    folder.mkdir()
    return folder


@pytest.fixture
def make_index(tmp_path):
    made: list[FileIndex] = []

    def factory(roots, **kwargs) -> FileIndex:
        kwargs.setdefault("clock", lambda: CLOCK)
        kwargs.setdefault("pause_s", 0)
        index = FileIndex(tmp_path / f"index{len(made)}.db", roots=roots, **kwargs)
        made.append(index)
        return index

    yield factory
    for index in made:
        index.close()


# ------------------------------------------------------------------ basics

def test_crawl_indexes_names_and_text(root, make_index):
    _write(root / "Tekstil_shartnoma.txt", "dogovor postavki tekstil")
    _write(root / "photo.jpg", "not really an image")
    index = make_index([str(root)])
    report = index.crawl()
    assert report["added"] == 2 and report["stopped"] is None
    assert _names(index) == ["Tekstil_shartnoma.txt", "photo.jpg"]
    assert _row(index, "photo.jpg")["has_text"] == 0
    assert _row(index, "photo.jpg")["kind"] == "image"


def test_default_budgets_are_the_section_8_numbers():
    b = Budgets()
    assert b.db_bytes == 2 * 1024 ** 3
    assert b.rows == 500_000
    assert b.day_write_bytes == 500 * 1024 ** 2
    assert b.hour_write_bytes == 100 * 1024 ** 2
    assert b.commit_every == 200
    assert b.max_file_bytes == 45 * 1024 ** 2
    assert b.max_text_bytes == 200 * 1024


def test_text_is_stored_up_to_the_byte_cap(root, make_index):
    _write(root / "long.txt", "слово " * 200)
    index = make_index([str(root)], budgets=Budgets(max_text_bytes=50))
    index.crawl()
    with index._lock:
        body = index._conn.execute("SELECT body FROM texts").fetchone()["body"]
    assert len(body.encode("utf-8")) <= 50


def test_files_over_the_size_limit_are_indexed_by_name_only(root, make_index):
    _write(root / "big.txt", "text that should not be extracted")
    index = make_index([str(root)], budgets=Budgets(max_file_bytes=10))
    index.crawl()
    assert _names(index) == ["big.txt"]
    assert _row(index, "big.txt")["has_text"] == 0


def test_unchanged_files_are_not_extracted_again(root, make_index, monkeypatch):
    _write(root / "a.txt", "one")
    _write(root / "b.txt", "two")
    index = make_index([str(root)])
    first = index.crawl()
    assert first["text_extracted"] == 2

    calls: list[str] = []
    real = crawl_mod.extract
    monkeypatch.setattr(crawl_mod, "extract", lambda p, limit: calls.append(p) or real(p, limit=limit))
    second = index.crawl()
    assert calls == []
    assert second["unchanged"] == 2


def test_a_deleted_file_is_removed_by_the_next_complete_pass(root, make_index):
    gone = _write(root / "gone.txt", "soon removed")
    _write(root / "stays.txt", "kept")
    index = make_index([str(root)])
    index.crawl()
    gone.unlink()
    index.crawl()
    assert _names(index) == ["stays.txt"]


# --------------------------------------------------------------- exclusions

def test_excluded_folders_are_skipped(root, make_index):
    _write(root / "ok.txt")
    _write(root / "Documents" / "ok2.txt")
    for folder in ("node_modules", ".git", "__pycache__", "site-packages", "AppData", "Windows"):
        _write(root / folder / f"bad_{folder.strip('._')}.txt")
    index = make_index([str(root)])
    index.crawl()
    assert _names(index) == ["ok.txt", "ok2.txt"]


# --------------------------------------------------------------------- depth

def test_depth_limit_is_ten_directories(root, make_index):
    _write(root / "f0.txt")
    folder = root
    for level in range(1, 13):  # f1 sits in a folder at depth 1, and so on to f12 at depth 12
        folder = folder / f"d{level}"
        _write(folder / f"f{level}.txt")
    index = make_index([str(root)])
    index.crawl()
    names = _names(index)
    assert {f"f{n}.txt" for n in range(0, 11)} <= set(names)
    assert "f11.txt" not in names and "f12.txt" not in names


# ------------------------------------------------------------ links

def _junction(link: Path, target: Path) -> bool:
    if sys.platform != "win32":
        return False
    done = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True,
    )
    return done.returncode == 0


def test_symlinks_and_junctions_are_never_followed(tmp_path, root, make_index):
    outside = _write(tmp_path / "outside" / "secret_plan.txt", "private")
    _write(root / "ok.txt")
    linked = False
    try:
        os.symlink(outside.parent, root / "sym", target_is_directory=True)
        linked = True
    except (OSError, NotImplementedError):
        pass
    junctioned = _junction(root / "junction", outside.parent)
    if not linked and not junctioned:
        pytest.skip("this machine cannot create a symlink or a junction")
    index = make_index([str(root)])
    index.crawl()
    assert _names(index) == ["ok.txt"]


def test_is_link_entry_flags_a_junction_but_not_a_plain_file_or_folder(tmp_path, root):
    from coworker import fs

    target = tmp_path / "target"
    _write(target / "a.txt")
    _write(root / "file.txt")
    (root / "plain").mkdir()
    entries = {e.name: e for e in os.scandir(root)}
    assert fs.is_link_entry(entries["file.txt"]) is False
    assert fs.is_link_entry(entries["plain"]) is False
    if not _junction(root / "junction", target):
        pytest.skip("this machine cannot create a junction")
    entries = {e.name: e for e in os.scandir(root)}
    assert fs.is_link_entry(entries["junction"]) is True
    assert [e.name for e in crawl_mod.scan_dir(str(root)) if e.is_dir] == ["plain"]


# ---------------------------------------------------------------- placeholders

@pytest.mark.parametrize("attribute", [0x1000, 0x40000, 0x400000])
def test_placeholders_are_indexed_by_name_and_never_opened(root, make_index, monkeypatch, attribute):
    cloud = _write(root / "Cloud_Shartnoma.txt", "text that lives in the cloud")
    monkeypatch.setattr(
        crawl_mod, "attributes_of",
        lambda entry: attribute if entry.name == "Cloud_Shartnoma.txt" else 0,
    )
    opened: list[str] = []
    real = crawl_mod.extract
    monkeypatch.setattr(crawl_mod, "extract", lambda p, limit: opened.append(p) or real(p, limit=limit))

    index = make_index([str(root)])
    report = index.crawl()
    assert str(cloud) not in opened
    assert report["placeholders"] == 1
    row = _row(index, "Cloud_Shartnoma.txt")
    assert row["placeholder"] == 1 and row["has_text"] == 0

    found = index.search("cloud")
    assert [r["name"] for r in found["results"]] == ["Cloud_Shartnoma.txt"]
    assert "snippet" not in found["results"][0]


# -------------------------------------------------------------------- budgets

def test_row_budget_stops_the_crawl(root, make_index):
    for n in range(6):
        _write(root / f"file{n}.txt")
    index = make_index([str(root)], budgets=Budgets(rows=3))
    report = index.crawl()
    assert len(_names(index)) == 3
    assert report["stopped"] == "rows"
    assert index.stats()["state"] == "budget"


def test_database_size_budget_stops_before_any_write(root, make_index):
    _write(root / "a.txt")
    index = make_index([str(root)], budgets=Budgets(db_bytes=1))
    report = index.crawl()
    assert report["stopped"] == "db_size"
    assert _names(index) == []


def test_hour_write_budget_admits_one_file_and_refuses_the_second(root, make_index):
    text = "x" * 300
    for n in range(3):
        _write(root / f"f{n}.txt", text)
    one_write = len(os.path.join(str(root), "f0.txt").encode()) + len("f0.txt") + len(text)
    index = make_index([str(root)], budgets=Budgets(hour_write_bytes=int(1.5 * one_write)))
    report = index.crawl()
    assert report["stopped"] == "hour_writes"
    assert len(_names(index)) == 1
    assert index.stats()["hour_writes"] <= int(1.5 * one_write)


def test_day_write_budget_stops_the_crawl(root, make_index):
    text = "y" * 300
    for n in range(3):
        _write(root / f"g{n}.txt", text)
    one_write = len(os.path.join(str(root), "g0.txt").encode()) + len("g0.txt") + len(text)
    index = make_index([str(root)], budgets=Budgets(day_write_bytes=int(1.5 * one_write)))
    report = index.crawl()
    assert report["stopped"] == "day_writes"
    assert len(_names(index)) == 1


def test_writes_commit_every_n_rows(root, make_index):
    for n in range(5):
        _write(root / f"c{n}.txt")
    batched = make_index([str(root)], budgets=Budgets(commit_every=2))
    batched.crawl()
    # begin of pass, a commit after rows 2 and 4, the flush, and the end of the pass.
    assert batched.commits == 5
    assert len(_names(batched)) == 5

    one_batch = make_index([str(root)], budgets=Budgets(commit_every=200))
    one_batch.crawl()
    assert one_batch.commits == 3


# ------------------------------------------------------------------ governor

def test_crawl_pauses_while_the_governor_refuses_and_then_finishes(root, make_index):
    for n in range(3):
        _write(root / f"p{n}.txt")
    governor = FakeGovernor(refusals=["paused", "throttled", "low_memory"])
    index = make_index([str(root)], governor=governor)
    report = index.crawl()
    assert report["pauses"] == 3
    assert report["stopped"] is None
    assert len(_names(index)) == 3
    assert set(governor.calls) == {"INDEX"}


def test_a_refused_step_is_retried_not_skipped(root, make_index):
    _write(root / "only.txt")
    governor = FakeGovernor(refusals=["paused"])
    index = make_index([str(root)], governor=governor)
    report = index.crawl()
    assert report["dirs_scanned"] == 1
    assert _names(index) == ["only.txt"]


def test_a_coroutine_governor_is_driven_to_completion(root, make_index):
    _write(root / "async.txt", "asynchronous")
    governor = AsyncGovernor()
    index = make_index([str(root)], governor=governor)
    report = index.crawl()
    assert governor.calls >= 2
    assert report["added"] == 1


def test_a_timed_out_extraction_still_indexes_the_name(root, make_index):
    _write(root / "slow.txt", "never extracted in time")
    governor = FakeGovernor(timeouts_on={60})  # the extraction limit is 60 s
    index = make_index([str(root)], governor=governor)
    report = index.crawl()
    assert report["timeouts"] == 1
    assert _names(index) == ["slow.txt"]
    assert _row(index, "slow.txt")["has_text"] == 0


def test_a_timed_out_extraction_is_retried_on_the_next_pass(root, make_index):
    _write(root / "slow.txt", "eventually extracted")
    index = make_index([str(root)], governor=FakeGovernor(timeouts_on={60}))
    index.crawl()
    assert _row(index, "slow.txt")["has_text"] == 0
    report = index.crawl(governor=FakeGovernor())
    assert report["text_extracted"] == 1
    assert _row(index, "slow.txt")["has_text"] == 1


def test_a_timed_out_folder_listing_does_not_prune_its_files(root, make_index):
    _write(root / "sub" / "keep.txt")
    _write(root / "top.txt")
    index = make_index([str(root)])
    index.crawl()
    assert _names(index) == ["keep.txt", "top.txt"]
    report = index.crawl(governor=FakeGovernor(timeouts_on={30}))  # 30 s is the folder-listing limit
    assert report["scan_timeouts"] >= 1
    assert _names(index) == ["keep.txt", "top.txt"]


def test_a_refusal_that_is_not_a_pause_ends_the_pass_with_its_code(root, make_index):
    _write(root / "a.txt")
    index = make_index([str(root)], governor=FakeGovernor(refusals=["locked_desktop"]))
    report = index.crawl()
    assert report["stopped"] == "locked_desktop"
    assert index.stats()["state"] == "stopped"


def test_stop_event_ends_the_crawl_between_steps(root, make_index):
    for n in range(4):
        _write(root / f"s{n}.txt")
    stop = threading.Event()
    stop.set()
    index = make_index([str(root)])
    report = index.crawl(stop)
    assert report["stopped"] == "stopped"
    assert _names(index) == []


# ----------------------------------------------------------------- search

@pytest.mark.parametrize("use_fts", [True, False])
def test_search_by_name_and_by_text_with_fts_or_like(root, make_index, use_fts):
    _write(root / "Tekstil_shartnoma.txt", "dogovor postavki tekstil")
    _write(root / "other.txt", "nothing relevant here")
    index = make_index([str(root)], use_fts=use_fts)
    index.crawl()
    assert index.fts_enabled is use_fts

    by_name = index.search("tekstil")
    assert by_name["source"] == "index"
    assert "Tekstil_shartnoma.txt" in [r["name"] for r in by_name["results"]]

    by_text = index.search("postavki")
    hit = by_text["results"][0]
    assert hit["name"] == "Tekstil_shartnoma.txt"
    assert "postavki" in hit["snippet"]

    translated = index.search("shartnoma")  # the Uzbek title, matched through the Russian word in the text
    assert "Tekstil_shartnoma.txt" in [r["name"] for r in translated["results"]]
    assert "other.txt" not in [r["name"] for r in index.search("tekstil")["results"]]


def test_results_are_capped_and_report_truncation(root, make_index):
    for n in range(3):
        _write(root / f"Shartnoma_{n}.txt")
    index = make_index([str(root)])
    index.crawl()
    found = index.search("shartnoma", limit=2)
    assert len(found["results"]) == 2
    assert found["truncated"] is True
    assert found["source"] == "index"


def test_root_argument_limits_index_results(root, make_index, tmp_path):
    _write(root / "a" / "Shartnoma.txt")
    _write(root / "b" / "Shartnoma.txt")
    index = make_index([str(root)])
    index.crawl()
    found = index.search("shartnoma", root=str(root / "a"))
    assert [r["path"] for r in found["results"]] == [str(root / "a" / "Shartnoma.txt")]


def test_empty_query_finds_nothing(root, make_index):
    _write(root / "a.txt")
    index = make_index([str(root)])
    index.crawl()
    assert index.search("   ") == {"results": [], "source": "index", "truncated": False}


def test_live_fallback_answers_when_the_index_is_empty(root, make_index):
    _write(root / "Shartnoma_live.docx")
    index = make_index([str(root)])  # not crawled yet
    found = index.search("shartnoma")
    assert found["source"] == "live"
    assert [r["name"] for r in found["results"]] == ["Shartnoma_live.docx"]
    assert found["results"][0]["size"] > 0


def test_live_fallback_stays_inside_the_index_roots(root, make_index, tmp_path):
    outside = tmp_path / "elsewhere"
    _write(outside / "Shartnoma_outside.docx")
    index = make_index([str(root)])
    assert index.search("shartnoma")["results"] == []


def test_live_fallback_honours_an_explicit_root(root, make_index, tmp_path):
    chosen = tmp_path / "chosen"
    _write(chosen / "Shartnoma_chosen.docx")
    index = make_index([str(root)])
    found = index.search("shartnoma", root=str(chosen))
    assert found["source"] == "live"
    assert [r["name"] for r in found["results"]] == ["Shartnoma_chosen.docx"]


def test_list_dir_returns_the_file_layer_codes(root, make_index):
    _write(root / "inside.txt")
    index = make_index([str(root)])
    assert index.list_dir(str(root / "missing"))["code"] == "path_invalid"
    listing = index.list_dir(str(root))
    assert "error" not in listing
    assert [f["path"] for f in listing["files"]] == [str(root / "inside.txt")]


# --------------------------------------------------------------- roots

def test_default_roots_are_user_folders_and_never_a_whole_disk(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    (home / "OneDrive" / "Documents").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OneDrive", raising=False)
    monkeypatch.delenv("OneDriveConsumer", raising=False)
    monkeypatch.delenv("OneDriveCommercial", raising=False)
    from coworker import fs
    monkeypatch.setattr(fs, "known_folders", lambda **_: {})

    roots = default_roots()
    assert str(home / "Desktop") in roots
    assert str(home / "OneDrive" / "Documents") in roots
    assert all(Path(r).name in USER_FOLDERS for r in roots)
    assert all(Path(r).parent != Path(r).anchor for r in roots)


# ----------------------------------------------------------- background

def test_start_runs_a_pass_in_the_background_and_stop_joins_it(root, make_index):
    _write(root / "bg1.txt")
    _write(root / "bg2.txt")
    index = make_index([str(root)], rescan_s=3600)
    stop = threading.Event()
    index.start(stop)
    deadline = time.monotonic() + 10
    while index.stats()["files"] < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert index.stats()["files"] == 2
    index.stop()
    assert index.stats()["running"] is False
    assert stop.is_set()


def test_stats_reports_the_documented_fields(root, make_index):
    index = make_index([str(root)])
    stats = index.stats()
    for key in ("files", "texts", "db_bytes", "fts", "roots", "state", "day_writes", "hour_writes", "commits"):
        assert key in stats


# ------------------------------------------- blocked names and protected folders

def test_a_file_with_a_blocked_name_is_never_read_or_indexed(root, make_index):
    _write(root / "bank passwords.txt", "login: alice / password: hunter2-bank")
    _write(root / "letter.txt", "a letter about the bank")
    index = make_index([str(root)])
    report = index.crawl()
    assert _row(index, "bank passwords.txt") is None
    assert _row(index, "letter.txt") is not None
    assert report["refused"] == 1
    assert report["text_extracted"] == 1


def test_a_search_never_returns_text_or_a_name_the_policy_blocks(root, make_index):
    _write(root / "bank passwords.txt", "login: alice / password: hunter2-bank")
    index = make_index([str(root)])
    index.crawl()
    assert index.search("hunter2")["results"] == []
    assert index.search("bank password")["results"] == []


def test_rows_from_before_the_rule_are_hidden_from_search_at_once(root, make_index):
    # An index written by an older version can hold a blocked file's text. Search
    # must not return it, even before the next complete pass removes the row.
    index = make_index([str(root)])
    index.upsert(FileRecord(
        path=str(root / "bank passwords.txt"), name="bank passwords.txt", folder=str(root),
        size=40, mtime=1.0, kind="text", text="login: alice / password: hunter2-bank",
    ))
    assert index.search("hunter2")["results"] == []
    assert index.search("bank password")["results"] == []


def test_the_next_complete_pass_removes_rows_for_blocked_names(root, make_index):
    index = make_index([str(root)])
    index.upsert(FileRecord(
        path=str(root / "bank passwords.txt"), name="bank passwords.txt", folder=str(root),
        size=40, mtime=1.0, kind="text", text="login: alice / password: hunter2-bank",
    ))
    _write(root / "letter.txt", "plain letter")
    index.crawl()
    assert _row(index, "bank passwords.txt") is None


def test_a_protected_folder_inside_an_index_root_is_not_read(root, make_index, monkeypatch):
    # COWORKER_HOME can sit under a user folder (a portable install).
    home = root / "portable-home"
    _write(home / "scratch.txt", "scratch plan details")
    monkeypatch.setenv("COWORKER_HOME", str(home))
    _write(root / "letter.txt", "plain letter")
    index = make_index([str(root)])
    report = index.crawl()
    assert _row(index, "scratch.txt") is None
    assert _row(index, "letter.txt") is not None
    assert report["refused"] >= 1
    assert index.search("scratch plan")["results"] == []


def test_a_folder_named_like_a_secret_does_not_hide_its_plain_files(root, make_index):
    # The rule judges a file by its own name, the same as preview_file does.
    _write(root / "Passwords" / "notes.txt", "a plain note about the office")
    index = make_index([str(root)])
    index.crawl()
    assert _row(index, "notes.txt") is not None


def test_live_fallback_drops_names_the_policy_blocks(root, make_index):
    _write(root / "bank passwords.txt")
    index = make_index([str(root)])  # not crawled: the live walk answers
    found = index.search("bank password")
    assert found["source"] == "live"
    assert found["results"] == []
