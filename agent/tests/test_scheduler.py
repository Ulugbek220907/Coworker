"""Scheduler: reminders, job runs, failure pauses, missed-run skips and panic.

The store is a small fake with the contract's semantics for due rows. Every
expected time is a naive local datetime turned into a timestamp, so the checks
hold in any time zone.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest

from coworker import scheduler as scheduler_module
from coworker.scheduler import FAILURE_LIMIT, JobFailed, Scheduler, advance, first_run, owner_chat
from coworker.store.db import Store

DAILY_9 = {"kind": "daily", "at": "09:00"}
EVERY_30 = {"kind": "interval", "minutes": 30}
EVERY_60 = {"kind": "interval", "minutes": 60}


def local(*parts: int) -> float:
    return datetime(*parts).timestamp()


MORNING = local(2026, 10, 8, 8, 0)
NINE = local(2026, 10, 8, 9, 0)
NEXT_NINE = local(2026, 10, 9, 9, 0)
DAY_AFTER_NINE = local(2026, 10, 10, 9, 0)


@pytest.fixture(autouse=True)
def coworker_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_HOME", str(tmp_path))


class FakeStore:
    """Reminders and jobs with the due semantics the contract promises."""

    def __init__(self) -> None:
        self.kv: dict = {"owner_chat_id": 42}
        self.reminders: dict[int, dict] = {}
        self.jobs: dict[int, dict] = {}
        self._ids = iter(range(1, 10_000))

    def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    def reminder_add(self, chat_id, text, due_ts, repeat=None):
        rid = next(self._ids)
        self.reminders[rid] = {"id": rid, "chat_id": chat_id, "text": text, "due_ts": due_ts, "done": False}
        return rid

    def reminders_due(self, now_ts):
        return [dict(r) for r in self.reminders.values() if not r["done"] and r["due_ts"] <= now_ts]

    def reminder_done(self, rid, next_due_ts=None):
        self.reminders[rid]["done"] = True

    def job_add(self, chat_id, name, instruction, schedule, next_run_ts):
        jid = next(self._ids)
        self.jobs[jid] = {
            "id": jid, "chat_id": chat_id, "name": name, "instruction": instruction,
            "schedule": schedule, "next_run_ts": next_run_ts, "failures": 0, "paused": False,
        }
        return jid

    def jobs_due(self, now_ts):
        return [dict(j) for j in self.jobs.values() if not j["paused"] and j["next_run_ts"] <= now_ts]

    def job_update(self, job_id, **fields):
        self.jobs[job_id].update(fields)


class FlakyStore(FakeStore):
    """Fails its first due-reminder query, then reports the second call."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.second_call = threading.Event()

    def reminders_due(self, now_ts):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("database is locked")
        self.second_call.set()
        return []


class FakeKill:
    def __init__(self, panic: bool = False) -> None:
        self.panic = panic

    def is_panic(self) -> bool:
        return self.panic


class Recorder:
    """Captures deliveries and job runs. Set ``error`` to make runs fail."""

    def __init__(self, result: str = "done") -> None:
        self.result = result
        self.error: Exception | None = None
        self.fail_delivery = False
        self.refuse_delivery = False   # Telegram answered, but did not accept the text
        self.delivered: list[tuple[int, str]] = []
        self.attempts: list[str] = []   # every text handed to the sender, delivered or not
        self.ran: list[int] = []

    def deliver(self, chat_id, text):
        self.attempts.append(text)
        if self.fail_delivery:
            raise OSError("network down")
        if self.refuse_delivery:
            return False
        self.delivered.append((chat_id, text))
        return True

    def run_job(self, job):
        self.ran.append(job["id"])
        if self.error is not None:
            raise self.error
        return self.result


def make(store, rec, kill=None, clock=None):
    return Scheduler(
        store,
        run_job=rec.run_job,
        deliver=rec.deliver,
        kill=kill or FakeKill(),
        clock=clock or time.time,
    )


# ------------------------------------------------------------ next-run maths

def test_daily_first_run_is_today_while_the_time_is_ahead():
    assert first_run(DAILY_9, MORNING) == NINE


def test_daily_first_run_rolls_to_tomorrow_after_the_time():
    assert first_run(DAILY_9, local(2026, 10, 8, 10, 0)) == NEXT_NINE


def test_daily_run_at_the_exact_time_counts_as_past():
    assert first_run(DAILY_9, NINE) == NEXT_NINE


def test_interval_first_run_waits_one_full_interval():
    assert first_run(EVERY_30, NINE) == NINE + 30 * 60


def test_daily_advance_after_a_slightly_late_run_moves_to_tomorrow():
    assert advance(DAILY_9, NINE, local(2026, 10, 8, 9, 0, 20)) == NEXT_NINE


def test_interval_advance_moves_to_the_next_slot_after_now():
    assert advance(EVERY_30, NINE, NINE) == NINE + 30 * 60
    assert advance(EVERY_30, NINE, local(2026, 10, 8, 9, 50)) == NINE + 60 * 60


def test_interval_advance_on_an_exact_slot_moves_past_it():
    assert advance(EVERY_30, NINE, local(2026, 10, 8, 9, 30)) == NINE + 60 * 60


# ------------------------------------------------------------------ reminders

def test_reminder_is_delivered_exactly_once_when_due():
    store, rec = FakeStore(), Recorder()
    store.reminder_add(42, "call Ali", local(2026, 10, 8, 10, 0))
    sched = make(store, rec)

    sched.tick(local(2026, 10, 8, 9, 59))
    assert rec.delivered == []

    sched.tick(local(2026, 10, 8, 10, 0))
    sched.tick(local(2026, 10, 8, 10, 1))
    assert rec.delivered == [(42, "Eslatma: call Ali")]


def test_reminder_that_is_hours_late_is_still_delivered():
    store, rec = FakeStore(), Recorder()
    store.reminder_add(42, "dori", local(2026, 10, 8, 6, 0))
    make(store, rec).tick(local(2026, 10, 8, 12, 0))
    assert rec.delivered == [(42, "Eslatma: dori")]


def test_reminder_whose_delivery_fails_stays_due_for_the_next_tick():
    store, rec = FakeStore(), Recorder()
    rid = store.reminder_add(42, "dori", local(2026, 10, 8, 10, 0))
    sched = make(store, rec)

    rec.fail_delivery = True
    sched.tick(local(2026, 10, 8, 10, 0))
    assert store.reminders[rid]["done"] is False

    rec.fail_delivery = False
    sched.tick(local(2026, 10, 8, 10, 0, 30))
    assert rec.delivered == [(42, "Eslatma: dori")]
    assert store.reminders[rid]["done"] is True


# ----------------------------------------------------------------------- jobs

def test_daily_job_runs_at_its_time_and_delivers_the_result():
    store, rec = FakeStore(), Recorder(result="Hisobot tayyor")
    jid = store.job_add(42, "hisobot", "kunlik hisobot", DAILY_9, first_run(DAILY_9, MORNING))
    make(store, rec).tick(local(2026, 10, 8, 9, 0, 5))

    assert rec.ran == [jid]
    assert rec.delivered == [(42, "«hisobot» ishi natijasi:\nHisobot tayyor")]
    assert store.jobs[jid]["next_run_ts"] == NEXT_NINE
    assert store.jobs[jid]["failures"] == 0


def test_job_with_an_empty_result_reports_plain_completion():
    store, rec = FakeStore(), Recorder(result="   ")
    store.job_add(42, "tozalash", "vaqtinchalik fayllar", DAILY_9, NINE)
    make(store, rec).tick(NINE)
    assert rec.delivered == [(42, "«tozalash» ishi bajarildi.")]


def test_job_that_is_not_due_does_not_run():
    store, rec = FakeStore(), Recorder()
    store.job_add(42, "hisobot", "kunlik", DAILY_9, NINE)
    make(store, rec).tick(NINE - 1)
    assert rec.ran == []


def test_interval_job_runs_each_slot_on_its_grid():
    store, rec = FakeStore(), Recorder()
    jid = store.job_add(42, "tekshiruv", "holatni tekshir", EVERY_30, NINE + 30 * 60)
    sched = make(store, rec)

    sched.tick(NINE + 30 * 60 + 1)
    assert store.jobs[jid]["next_run_ts"] == NINE + 60 * 60
    sched.tick(NINE + 60 * 60 + 1)
    assert rec.ran == [jid, jid]


def test_job_failing_three_times_in_a_row_is_paused_and_error_text_is_not_sent():
    store, rec = FakeStore(), Recorder()
    rec.error = RuntimeError("provider 401: key sk-secret")
    jid = store.job_add(42, "hisobot", "kunlik", DAILY_9, NINE)
    sched = make(store, rec)

    sched.tick(NINE)
    sched.tick(NEXT_NINE)
    sched.tick(DAY_AFTER_NINE)

    assert store.jobs[jid]["paused"] is True
    assert store.jobs[jid]["failures"] == FAILURE_LIMIT
    assert "to'xtatildi" in rec.delivered[-1][1]
    assert all("sk-secret" not in text and "401" not in text for _, text in rec.delivered)

    sched.tick(local(2026, 10, 11, 9, 0))
    assert len(rec.ran) == FAILURE_LIMIT


def test_one_success_resets_the_failure_count():
    store, rec = FakeStore(), Recorder()
    jid = store.job_add(42, "hisobot", "kunlik", DAILY_9, NINE)
    sched = make(store, rec)

    rec.error = RuntimeError("timeout")
    sched.tick(NINE)
    sched.tick(NEXT_NINE)
    assert store.jobs[jid]["failures"] == 2

    rec.error = None
    sched.tick(DAY_AFTER_NINE)
    assert store.jobs[jid]["failures"] == 0
    assert store.jobs[jid]["paused"] is False


def test_run_more_than_two_hours_late_is_skipped_and_reported():
    store, rec = FakeStore(), Recorder()
    jid = store.job_add(42, "hisobot", "kunlik", DAILY_9, NINE)
    make(store, rec).tick(local(2026, 10, 8, 12, 30))

    assert rec.ran == []
    assert store.jobs[jid]["next_run_ts"] == NEXT_NINE
    assert rec.delivered[0][0] == 42
    assert "o'tkazib yuborildi" in rec.delivered[0][1]
    assert "3 soat 30 daqiqa" in rec.delivered[0][1]


def test_run_exactly_two_hours_late_still_runs():
    store, rec = FakeStore(), Recorder()
    jid = store.job_add(42, "hisobot", "kunlik", DAILY_9, NINE)
    make(store, rec).tick(local(2026, 10, 8, 11, 0))
    assert rec.ran == [jid]


def test_skipped_interval_run_lands_on_the_grid_not_a_fresh_interval():
    store, rec = FakeStore(), Recorder()
    jid = store.job_add(42, "tekshiruv", "holat", EVERY_60, NINE)
    make(store, rec).tick(local(2026, 10, 8, 12, 30))

    assert rec.ran == []
    assert store.jobs[jid]["next_run_ts"] == local(2026, 10, 8, 13, 0)


def test_paused_job_is_never_run_even_if_the_store_returns_it():
    store, rec = FakeStore(), Recorder()
    jid = store.job_add(42, "hisobot", "kunlik", DAILY_9, NINE)
    store.jobs[jid]["paused"] = True

    class Careless(FakeStore):
        def jobs_due(self, now_ts):
            return [dict(store.jobs[jid])]

    sched = Scheduler(Careless(), run_job=rec.run_job, deliver=rec.deliver, kill=FakeKill())
    sched.tick(NINE)
    assert rec.ran == []


# ---------------------------------------------------------- panic and clock

def test_panic_pauses_reminders_and_jobs_until_it_is_cleared():
    store, rec, kill = FakeStore(), Recorder(), FakeKill(panic=True)
    store.reminder_add(42, "dori", NINE)
    jid = store.job_add(42, "hisobot", "kunlik", DAILY_9, NINE)
    sched = make(store, rec, kill=kill)

    sched.tick(NINE + 60)
    assert rec.delivered == []
    assert rec.ran == []
    assert store.jobs[jid]["next_run_ts"] == NINE

    kill.panic = False
    sched.tick(NINE + 120)
    assert rec.delivered[0] == (42, "Eslatma: dori")
    assert rec.ran == [jid]


def test_tick_reads_the_clock_when_no_time_is_given():
    store, rec = FakeStore(), Recorder()
    store.reminder_add(42, "dori", NINE)
    make(store, rec, clock=lambda: NINE + 1).tick()
    assert rec.delivered == [(42, "Eslatma: dori")]


def test_owner_chat_prefers_the_live_chat_then_the_pinned_owner():
    store = FakeStore()
    assert owner_chat(store, 7) == 7
    assert owner_chat(store, None) == 42
    store.kv.clear()
    assert owner_chat(store, None) is None


# ------------------------------------------------------------------ thread

def test_start_delivers_at_once_and_stop_ends_the_thread():
    store, rec = FakeStore(), Recorder()
    store.reminder_add(42, "dori", time.time() - 1)
    delivered = threading.Event()

    sched = Scheduler(
        store,
        run_job=rec.run_job,
        deliver=lambda chat_id, text: delivered.set() or True,
        kill=FakeKill(),
    )
    sched.start()
    try:
        assert delivered.wait(timeout=5)
    finally:
        sched.stop()
    assert not any(t.name == "scheduler" for t in threading.enumerate())


@pytest.fixture
def real_store(tmp_path):
    return Store(tmp_path / "coworker.db", use_fts=False)


def test_reminder_is_delivered_once_through_the_real_store(real_store):
    real_store.reminder_add(42, "dori", NINE)
    rec = Recorder()
    sched = make(real_store, rec)
    sched.tick(NINE)
    sched.tick(NINE + 60)
    assert rec.delivered == [(42, "Eslatma: dori")]


def test_job_pause_is_persisted_and_the_job_leaves_the_due_list(real_store):
    rec = Recorder()
    rec.error = RuntimeError("boom")
    real_store.job_add(42, "hisobot", "kunlik", DAILY_9, NINE)
    sched = make(real_store, rec)
    sched.tick(NINE)
    sched.tick(NEXT_NINE)
    sched.tick(DAY_AFTER_NINE)

    [row] = real_store.jobs_list(42)
    assert row["paused"] is True
    assert row["failures"] == FAILURE_LIMIT
    assert row["schedule"] == DAILY_9
    assert real_store.jobs_due(local(2026, 10, 11, 9, 0)) == []

    sched.tick(local(2026, 10, 11, 9, 0))
    assert len(rec.ran) == FAILURE_LIMIT


def test_a_failing_tick_does_not_stop_the_thread(monkeypatch):
    monkeypatch.setattr(scheduler_module, "TICK_S", 0.01)
    store, rec = FlakyStore(), Recorder()
    sched = make(store, rec)
    sched.start()
    try:
        assert store.second_call.wait(timeout=5)
    finally:
        sched.stop()
    assert store.calls >= 2


# ------------------------------------------------- sends and runs that do not finish

def test_a_reminder_telegram_refuses_stays_due_and_is_not_closed():
    store, rec = FakeStore(), Recorder()
    rid = store.reminder_add(42, "dori", local(2026, 10, 8, 10, 0))
    rec.refuse_delivery = True

    make(store, rec).tick(local(2026, 10, 8, 10, 0))

    assert store.reminders[rid]["done"] is False
    assert rec.delivered == []


def test_a_refused_reminder_is_retried_after_a_pause_not_on_every_tick():
    store, rec = FakeStore(), Recorder()
    rid = store.reminder_add(42, "dori", local(2026, 10, 8, 10, 0))
    rec.refuse_delivery = True
    sched = make(store, rec)
    sched.tick(local(2026, 10, 8, 10, 0))

    sched.tick(local(2026, 10, 8, 10, 0, 10))   # inside the first pause
    assert len(rec.attempts) == 1

    rec.refuse_delivery = False
    sched.tick(local(2026, 10, 8, 10, 0, 30))   # the pause is over
    assert store.reminders[rid]["done"] is True
    assert rec.delivered == [(42, "Eslatma: dori")]


def test_a_run_stopped_by_the_daily_budget_counts_as_a_failure_not_a_success():
    store, rec = FakeStore(), Recorder()
    rec.error = JobFailed("bugungi AI limiti tugadi")
    jid = store.job_add(42, "hisobot", "kunlik", DAILY_9, NINE)

    make(store, rec).tick(NINE)

    assert store.jobs[jid]["failures"] == 1
    text = rec.delivered[-1][1]
    assert "bajarilmadi" in text and "bugungi AI limiti tugadi" in text and "(1/3)" in text
    assert "natijasi" not in text and "bajarildi" not in text


def test_repeated_budget_stops_pause_the_job_after_the_limit():
    store, rec = FakeStore(), Recorder()
    rec.error = JobFailed("bugungi AI limiti tugadi")
    jid = store.job_add(42, "hisobot", "kunlik", DAILY_9, NINE)
    sched = make(store, rec)

    sched.tick(NINE)
    sched.tick(NEXT_NINE)
    sched.tick(DAY_AFTER_NINE)

    assert store.jobs[jid]["paused"] is True
    assert "to'xtatildi" in rec.delivered[-1][1]


def test_a_completed_run_after_failures_clears_the_count():
    store, rec = FakeStore(), Recorder()
    jid = store.job_add(42, "hisobot", "kunlik", DAILY_9, NINE)
    sched = make(store, rec)
    rec.error = JobFailed("AI bilan aloqa yo'q")
    sched.tick(NINE)
    assert store.jobs[jid]["failures"] == 1

    rec.error = None
    sched.tick(NEXT_NINE)

    assert store.jobs[jid]["failures"] == 0
