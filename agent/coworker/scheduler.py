"""The clock of Coworker: reminders, scheduled jobs and their failure rules.

A daemon thread wakes every ``TICK_S`` seconds and asks the store what is due.
Two kinds of work come out of it:

* Reminders are sent as fixed text through ``deliver``. No model call is made,
  so a reminder still arrives when the model provider is down.
* Jobs run through ``run_job``. The runtime wires that to the orchestrator with
  actor ``scheduler`` and autonomy capped at AR. The text it returns is
  delivered to the owner, because an unattended run has nowhere else to report.

Rules and the reason for each:

* Work runs one item at a time in the tick thread. A slow job delays the next
  tick, but two runs of the same job can never overlap.
* A run that does not complete counts as a failure, whatever the cause. A job
  that fails ``FAILURE_LIMIT`` times in a row is paused, so a broken instruction
  does not spend the budget every 30 seconds. The owner is told. A run stopped by
  the day's AI budget raises ``JobFailed`` and counts the same way.
* A run more than ``MAX_LATENESS_S`` late is skipped, not run. A job that was
  due while the laptop slept would otherwise fire at an hour nobody asked for.
  The skip is reported so the owner knows the run did not happen.
* A reminder is closed only after ``deliver`` reports that Telegram accepted it.
  A failed send leaves it due, and retries back off, so a reminder is not lost
  while the connection is down. Reminders have no lateness limit: a late
  reminder is still wanted.
* Panic pauses ticks. Resuming is local only, so nothing fires from a panicked
  state.

Schedule dicts stored with a job:

    {"kind": "daily", "at": "HH:MM"}     local wall-clock time each day
    {"kind": "interval", "minutes": N}   every N minutes, on a grid from the first run
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

log = logging.getLogger("scheduler")

TICK_S = 30.0
MAX_LATENESS_S = 2 * 3600
FAILURE_LIMIT = 3
RETRY_MAX_S = 300.0       # a reminder's retries are spaced at most this far apart


class JobFailed(Exception):
    """A job run that did not complete. The message is the owner-facing reason."""


def local_iso(ts: float) -> str:
    """A timestamp as local wall-clock time at minute precision, for owner-facing text and tool output."""
    return datetime.fromtimestamp(ts).isoformat(timespec="minutes")


def owner_chat(store: Any, chat_id: Optional[int]) -> Optional[int]:
    """The chat that owns a write.

    Unattended runs carry no chat, so they fall back to the paired owner's chat,
    which the pairing step pins in kv. None means nobody has paired yet.
    """
    if chat_id is not None:
        return chat_id
    value = store.kv_get("owner_chat_id")
    return None if value is None else int(value)


def first_run(schedule: dict, now: float) -> float:
    """The first time a new job is due. An interval job waits one full interval."""
    if schedule["kind"] == "interval":
        return now + schedule["minutes"] * 60
    return _next_daily(schedule["at"], now)


def advance(schedule: dict, scheduled: float, now: float) -> float:
    """The next run after one that was due at ``scheduled`` and handled at ``now``.

    An interval job stays on the grid anchored at its first run. After a skip the
    next run lands on that grid, not at ``now`` plus a full interval, so the
    rhythm the owner chose does not drift.
    """
    if schedule["kind"] == "interval":
        step = schedule["minutes"] * 60
        steps = max(0, math.floor((now - scheduled) / step)) + 1
        return scheduled + steps * step
    return _next_daily(schedule["at"], max(now, scheduled))


def _next_daily(at: str, after: float) -> float:
    """The first local HH:MM strictly after ``after``.

    The day step is done on the naive wall clock, so a daylight-saving change
    keeps the job at the same local time of day.
    """
    hour, minute = (int(part) for part in at.split(":"))
    candidate = datetime.fromtimestamp(after).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= after:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def _late_text(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours} soat {rest // 60} daqiqa"


def _retry_delay(attempts: int) -> float:
    """Seconds to wait after the ``attempts``-th failed send: one tick, then doubling, capped."""
    return min(RETRY_MAX_S, TICK_S * 2 ** max(0, attempts - 1))


class Scheduler:
    """Delivers due reminders and runs due jobs. ``tick`` is the whole behaviour; ``start`` only calls it on a timer."""

    def __init__(
        self,
        store: Any,
        *,
        run_job: Callable[[dict], str],
        deliver: Callable[[int, str], bool],
        kill: Any,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._run_job = run_job
        self._deliver = deliver
        self._kill = kill
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._retries: dict[int, tuple[int, float]] = {}   # reminder id -> (failed sends, next try)

    def tick(self, now: Optional[float] = None) -> None:
        if self._kill.is_panic():
            return
        now = self._clock() if now is None else now
        self._deliver_reminders(now)
        self._run_jobs(now)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        # The first tick runs at once, so reminders that came due while the
        # laptop was off are delivered as soon as Coworker starts.
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            if self._stop.wait(TICK_S):
                return

    def _deliver_reminders(self, now: float) -> None:
        for reminder in self._store.reminders_due(now):
            rid = reminder["id"]
            attempts, retry_at = self._retries.get(rid, (0, 0.0))
            if now < retry_at:
                continue
            try:
                delivered = bool(self._deliver(reminder["chat_id"], f"Eslatma: {reminder['text']}"))
                if delivered:
                    self._store.reminder_done(rid)
            except Exception:
                log.exception("reminder %s could not be sent", rid)
                delivered = False
            if delivered:
                self._retries.pop(rid, None)
                continue
            # Not closed: the reminder stays due and is tried again after a pause.
            attempts += 1
            self._retries[rid] = (attempts, now + _retry_delay(attempts))
            log.warning("reminder %s not delivered (attempt %d); it stays due", rid, attempts)

    def _run_jobs(self, now: float) -> None:
        for job in self._store.jobs_due(now):
            # The store query should already exclude paused jobs. A paused job must
            # never run, so the check is repeated here rather than trusted.
            if job.get("paused"):
                continue
            try:
                self._run_one(job, now)
            except Exception:
                log.exception("job %s could not be handled", job["id"])

    def _run_one(self, job: dict, now: float) -> None:
        schedule = job["schedule"]
        scheduled = float(job["next_run_ts"])
        chat_id = job["chat_id"]
        name = job["name"]
        next_ts = advance(schedule, scheduled, now)

        if now - scheduled > MAX_LATENESS_S:
            self._store.job_update(job["id"], next_run_ts=next_ts)
            self._deliver(
                chat_id,
                f"«{name}» ishi {_late_text(now - scheduled)} kechikdi, o'tkazib yuborildi. "
                f"Keyingi ish: {local_iso(next_ts)}.",
            )
            return

        try:
            result = self._run_job(job)
        except JobFailed as exc:
            log.info("job %s did not complete: %s", job["id"], exc)
            self._failed(job, next_ts, f"«{name}» ishi bajarilmadi: {exc}")
            return
        except Exception as exc:
            # The exception text is not shown or logged: provider errors can carry
            # request details, so only the exception type is recorded.
            log.warning("job %s failed (%s)", job["id"], type(exc).__name__)
            self._failed(job, next_ts, f"«{name}» ishi xato berdi")
            return

        self._store.job_update(job["id"], next_run_ts=next_ts, failures=0)
        text = str(result or "").strip()
        self._deliver(chat_id, f"«{name}» ishi natijasi:\n{text}" if text else f"«{name}» ishi bajarildi.")

    def _failed(self, job: dict, next_ts: float, message: str) -> None:
        """Count one run that did not complete, pause the job at the limit, and tell the owner."""
        failures = int(job.get("failures") or 0) + 1
        paused = failures >= FAILURE_LIMIT
        self._store.job_update(job["id"], next_run_ts=next_ts, failures=failures, paused=paused)
        if paused:
            self._deliver(job["chat_id"], f"«{job['name']}» ishi {FAILURE_LIMIT} marta ketma-ket xato berdi va to'xtatildi.")
        else:
            self._deliver(job["chat_id"], f"{message} ({failures}/{FAILURE_LIMIT}).")
