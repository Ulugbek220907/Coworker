"""Store: CRUD for every table, FTS and scan-fallback search, and the connection rules."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from coworker.store import Store, StoreError
from coworker.store import migrations


# ----------------------------------------------------------------- kv and turns

def test_kv_round_trip_and_default(store: Store) -> None:
    assert store.kv_get("missing") is None
    assert store.kv_get("missing", 7) == 7
    store.kv_set("cfg", {"a": [1, 2], "b": "тест"})
    assert store.kv_get("cfg") == {"a": [1, 2], "b": "тест"}
    store.kv_set("cfg", None)
    assert store.kv_get("cfg", "fallback") is None  # stored null is a value, not a miss


def test_kv_increment_counts_every_concurrent_bump(store: Store) -> None:
    def bump() -> None:
        for _ in range(20):
            store.kv_increment("n")

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.kv_get("n") == 80


def test_turns_recent_is_oldest_first_and_limited(store: Store) -> None:
    for i in range(5):
        store.turn_add(1, "user" if i % 2 == 0 else "assistant", f"m{i}")
    store.turn_add(2, "user", "other chat")
    assert [t["content"] for t in store.turns_recent(1, limit=3)] == ["m2", "m3", "m4"]
    assert [t["content"] for t in store.turns_recent(2)] == ["other chat"]


def test_turns_clear_hides_one_chat_and_removes_nothing(store: Store) -> None:
    store.turn_add(1, "user", "a")
    store.turn_add(2, "user", "b")
    store.turns_clear(1)
    assert store.turns_recent(1) == []
    assert [t["content"] for t in store.turns_recent(2)] == ["b"]
    assert store._read("SELECT COUNT(*) AS n FROM turns")[0]["n"] == 2


def test_summary_defaults_to_empty_and_overwrites(store: Store) -> None:
    assert store.summary_get(1) == ""
    store.summary_set(1, "first")
    store.summary_set(1, "second")
    assert store.summary_get(1) == "second"


# ------------------------------------------------------------------ facts

def test_facts_list_is_newest_first_with_flags(store: Store) -> None:
    first = store.fact_add(1, "shartnomalar D:/Ishxona ichida")
    second = store.fact_add(1, "«zavod» = Tekstil zavodi", kind="alias", untrusted=True)
    rows = store.facts_list(1)
    assert [r["id"] for r in rows] == [second, first]
    assert rows[0]["untrusted"] is True and rows[0]["kind"] == "alias"
    assert rows[1]["untrusted"] is False
    assert store.facts_list(2) == []


def test_fact_forget_hides_matches_and_an_empty_needle_matches_nothing(store: Store) -> None:
    store.fact_add(1, "Ali telefoni 1")
    store.fact_add(1, "Vali telefoni 2")
    store.fact_add(1, "Shartnoma")
    assert store.fact_forget(1, "   ") == 0
    assert store.fact_forget(2, "shart") == 0  # other chats are untouched
    assert store.fact_forget(1, "TELEFON") == 2  # matching ignores case
    assert [f["text"] for f in store.facts_list(1)] == ["Shartnoma"]
    assert store.fact_forget(1, "telefon") == 0  # already hidden


def test_fts_search_matches_all_words_in_any_script(store: Store) -> None:
    assert store.fts_enabled
    store.fact_add(1, "Tekstil zavodi D:/Ishxona/zavod ichida")
    store.fact_add(1, "Текстильная фабрика на складе")
    store.fact_add(1, "Shartnomalar papkasi")
    assert [h["text"] for h in store.facts_search(1, "тексТИЛЬ")] == ["Текстильная фабрика на складе"]
    assert [h["text"] for h in store.facts_search(1, "zavod")] == ["Tekstil zavodi D:/Ishxona/zavod ichida"]
    assert store.facts_search(1, "tekst")  # prefix match
    assert store.facts_search(1, "zavod papka") == []  # every word must match
    assert store.facts_search(1, "   ") == []
    assert store.facts_search(1, 'x" OR "') == []  # quoting keeps odd input from breaking the query


def test_forgotten_facts_do_not_match_fts(store: Store) -> None:
    store.fact_add(1, "Vali telefoni")
    store.fact_forget(1, "vali")
    assert store.facts_search(1, "telefoni") == []


def test_scan_fallback_returns_the_same_rows_as_fts(tmp_path: Path) -> None:
    db = Store(tmp_path / "scan.db", use_fts=False)
    try:
        assert not db.fts_enabled
        db.fact_add(1, "Tekstil zavodi")
        db.fact_add(1, "Текстильная фабрика")
        db.fact_add(1, "Shartnoma")
        assert [h["text"] for h in db.facts_search(1, "ТЕКСТИЛЬ")] == ["Текстильная фабрика"]
        assert [h["text"] for h in db.facts_search(1, "tekstil")] == ["Tekstil zavodi"]
        assert db.facts_search(1, "tekstil shartnoma") == []
        assert db.facts_search(1, "") == []
    finally:
        db.close()


def test_database_without_fts5_is_created_and_searched_by_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrations, "fts5_available", lambda conn: False)
    db = Store(tmp_path / "nofts.db")
    try:
        assert db.fts_enabled is False
        assert db._read("SELECT name FROM sqlite_master WHERE name = 'facts_fts'") == []
        db.fact_add(1, "Shartnomalar papkasi")
        db.note_add(1, "Yig'ilish", "Reja: shartnomalar")
        assert [f["text"] for f in db.facts_search(1, "shartnomalar")] == ["Shartnomalar papkasi"]
        assert [n["title"] for n in db.notes_search(1, "reja")] == ["Yig'ilish"]
    finally:
        db.close()


# ----------------------------------------------------------------- deliveries

def test_delivered_lists_each_path_once_newest_first(store: Store) -> None:
    store.delivered_add(1, "D:/a.pdf", "a.pdf")
    store.delivered_add(1, "D:/b.xlsx", "b.xlsx")
    store.delivered_add(1, "D:/a.pdf", "a.pdf")
    assert [r["path"] for r in store.delivered_recent(1)] == ["D:/a.pdf", "D:/b.xlsx"]
    assert store.delivered_recent(1, limit=1)[0]["path"] == "D:/a.pdf"
    assert store.delivered_recent(2) == []


# ---------------------------------------------------------------- notes, tasks

def test_notes_crud_and_fts_search(store: Store) -> None:
    older = store.note_add(1, "Yig'ilish", "Shartnoma imzolash, seshanba")
    newer = store.note_add(1, "Reja", "Тексты для фабрики")
    store.note_add(2, "Boshqa", "shartnoma")
    assert [n["id"] for n in store.notes_list(1)] == [newer, older]
    assert [n["id"] for n in store.notes_list(1, limit=1)] == [newer]
    assert [n["id"] for n in store.notes_search(1, "shartnoma")] == [older]
    assert [n["id"] for n in store.notes_search(1, "фабрик")] == [newer]  # the shared stem, as a prefix
    assert store.notes_search(1, "") == []


def test_notes_scan_fallback_matches_title_and_body(tmp_path: Path) -> None:
    db = Store(tmp_path / "notes.db", use_fts=False)
    try:
        note = db.note_add(1, "Yig'ilish", "Reja: Тексты")
        assert [n["id"] for n in db.notes_search(1, "yig'ilish reja")] == [note]
        assert [n["id"] for n in db.notes_search(1, "тексты")] == [note]
    finally:
        db.close()


def test_tasks_are_ordered_by_due_date_and_close_once(store: Store) -> None:
    later = store.task_add(1, "Hisobot", due_ts=2000.0)
    sooner = store.task_add(1, "Qo'ng'iroq", due_ts=1000.0)
    undated = store.task_add(1, "Kitob")
    assert [t["id"] for t in store.tasks_list(1)] == [sooner, later, undated]
    assert store.task_done(1, later) is True
    assert store.task_done(1, later) is False
    assert store.task_done(2, sooner) is False  # another chat cannot close it
    assert [t["id"] for t in store.tasks_list(1)] == [sooner, undated]
    all_tasks = store.tasks_list(1, open_only=False)
    assert len(all_tasks) == 3
    assert [t["done"] for t in all_tasks if t["id"] == later] == [True]


# ------------------------------------------------------------ reminders, jobs

def test_reminders_due_repeat_done_and_cancel(store: Store) -> None:
    once = store.reminder_add(1, "Dori", due_ts=100.0)
    repeating = store.reminder_add(1, "Suv ich", due_ts=50.0, repeat={"every_s": 3600})
    later = store.reminder_add(1, "Keyinroq", due_ts=500.0)
    due = store.reminders_due(120.0)
    assert [r["id"] for r in due] == [repeating, once]
    assert due[0]["repeat"] == {"every_s": 3600}
    store.reminder_done(repeating, next_due_ts=3650.0)
    assert [r["id"] for r in store.reminders_due(120.0)] == [once]
    store.reminder_done(once)
    assert store.reminders_due(120.0) == []
    assert store.reminder_cancel(2, later) is False
    assert store.reminder_cancel(1, later) is True
    assert store.reminder_cancel(1, later) is False
    assert [r["id"] for r in store.reminders_list(1)] == [repeating]


def test_jobs_due_update_and_disable(store: Store) -> None:
    job = store.job_add(1, "Hisobot", "Tayyorla", {"kind": "daily", "at": "08:00"}, next_run_ts=100.0)
    paused = store.job_add(1, "Pauza", "x", {"kind": "daily"}, next_run_ts=100.0)
    store.job_update(paused, paused=True)
    due = store.jobs_due(150.0)
    assert [j["id"] for j in due] == [job]
    assert due[0]["schedule"] == {"kind": "daily", "at": "08:00"}
    store.job_update(job, next_run_ts=200.0, failures=2, last_status="ok", schedule={"kind": "weekly"})
    assert store.jobs_due(150.0) == []
    row = next(j for j in store.jobs_list(1) if j["id"] == job)
    assert row["failures"] == 2
    assert row["schedule"] == {"kind": "weekly"}
    assert row["enabled"] is True
    with pytest.raises(ValueError):
        store.job_update(job, instruction="changed")  # not on the updatable list
    assert store.job_disable(2, job) is False
    assert store.job_disable(1, job) is True
    assert [j["id"] for j in store.jobs_list(1)] == [paused]


# ------------------------------------------------------------- connection rules

def test_connection_pragmas_are_set(store: Store) -> None:
    assert store._one("PRAGMA journal_mode")[0] == "wal"
    assert store._one("PRAGMA foreign_keys")[0] == 1
    assert store._one("PRAGMA busy_timeout")[0] == 5000


def test_schema_versions_are_recorded_once(store_path: Path) -> None:
    first = Store(store_path)
    first.close()
    again = Store(store_path)
    try:
        versions = [row["version"] for row in again._read("SELECT version FROM schema_version ORDER BY version")]
        assert versions == [1, 2]
    finally:
        again.close()


def test_data_survives_reopening_the_file(store_path: Path) -> None:
    first = Store(store_path)
    first.fact_add(1, "saqlansin")
    first.kv_set("k", 1)
    first.close()
    second = Store(store_path)
    try:
        assert [f["text"] for f in second.facts_list(1)] == ["saqlansin"]
        assert second.kv_get("k") == 1
    finally:
        second.close()


def test_failed_transaction_rolls_back(store: Store) -> None:
    with pytest.raises(RuntimeError):
        with store.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('half', '1')")
            raise RuntimeError("boom")
    assert store.kv_get("half") is None


def test_writers_on_many_threads_all_land(store: Store) -> None:
    def writer(chat_id: int) -> None:
        for i in range(25):
            store.turn_add(chat_id, "user", f"{chat_id}-{i}")

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for chat_id in range(4):
        assert len(store.turns_recent(chat_id, limit=100)) == 25


def test_closed_store_refuses_use(store_path: Path) -> None:
    db = Store(store_path)
    db.close()
    with pytest.raises(StoreError):
        db.kv_get("x")
