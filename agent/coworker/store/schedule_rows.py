"""Reminders and scheduled jobs.

A reminder is delivered as fixed text, so it needs no model call. A job runs an
instruction later and carries a frozen plan. Both are closed by status, never
removed, so the scheduler's history stays readable.

``job_update`` only writes columns on a fixed list. The column names reach the
SQL text, so accepting arbitrary names would be an injection path.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .base import StoreBase, row_dict

_JOB_FIELDS = frozenset({
    "next_run_ts", "failures", "paused", "enabled", "last_run_ts", "last_status", "schedule",
})
_REMINDER_JSON = ("repeat",)
_JOB_JSON = ("schedule",)
_JOB_FLAGS = ("enabled", "paused")


class ScheduleRows(StoreBase):
    # -------------------------------------------------------------- reminders

    def reminder_add(self, chat_id: int, text: str, due_ts: float, repeat: Any = None) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO reminders (chat_id, text, due_ts, repeat, status, created_at)"
                " VALUES (?, ?, ?, ?, 'active', ?)",
                (chat_id, text, due_ts, None if repeat is None else json.dumps(repeat, ensure_ascii=False),
                 time.time()),
            )
            return int(cur.lastrowid or 0)

    def reminders_due(self, now_ts: float) -> list[dict[str, Any]]:
        rows = self._read(
            "SELECT id, chat_id, text, due_ts, repeat, status, created_at FROM reminders"
            " WHERE status = 'active' AND due_ts <= ? ORDER BY due_ts, id",
            (now_ts,),
        )
        return [row_dict(row, json_keys=_REMINDER_JSON) for row in rows]

    def reminder_done(self, id: int, next_due_ts: float | None = None) -> None:
        """Finish a reminder, or move a repeating one to its next due time."""
        with self.transaction() as conn:
            if next_due_ts is None:
                conn.execute("UPDATE reminders SET status = 'done' WHERE id = ? AND status = 'active'", (id,))
            else:
                conn.execute(
                    "UPDATE reminders SET due_ts = ? WHERE id = ? AND status = 'active'",
                    (next_due_ts, id),
                )

    def reminders_list(self, chat_id: int) -> list[dict[str, Any]]:
        rows = self._read(
            "SELECT id, chat_id, text, due_ts, repeat, status, created_at FROM reminders"
            " WHERE chat_id = ? AND status = 'active' ORDER BY due_ts, id",
            (chat_id,),
        )
        return [row_dict(row, json_keys=_REMINDER_JSON) for row in rows]

    def reminder_cancel(self, chat_id: int, id: int) -> bool:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE reminders SET status = 'cancelled'"
                " WHERE chat_id = ? AND id = ? AND status = 'active'",
                (chat_id, id),
            )
            return cur.rowcount == 1

    # ------------------------------------------------------------------- jobs

    def job_add(
        self,
        chat_id: int,
        name: str,
        instruction: str,
        schedule: dict[str, Any],
        next_run_ts: float,
    ) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO jobs (chat_id, name, instruction, schedule, next_run_ts, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, name, instruction, json.dumps(schedule, ensure_ascii=False), next_run_ts,
                 time.time()),
            )
            return int(cur.lastrowid or 0)

    def jobs_due(self, now_ts: float) -> list[dict[str, Any]]:
        """Enabled, unpaused jobs whose next run time has passed."""
        rows = self._read(
            "SELECT * FROM jobs WHERE enabled = 1 AND paused = 0 AND next_run_ts <= ?"
            " ORDER BY next_run_ts, id",
            (now_ts,),
        )
        return [row_dict(row, json_keys=_JOB_JSON, flag_keys=_JOB_FLAGS) for row in rows]

    def job_update(self, id: int, **fields: Any) -> None:
        unknown = set(fields) - _JOB_FIELDS
        if unknown:
            raise ValueError(f"job fields that cannot be updated: {sorted(unknown)}")
        if not fields:
            return
        assignments: list[str] = []
        values: list[Any] = []
        for name in sorted(fields):
            value = fields[name]
            if name == "schedule":
                value = json.dumps(value, ensure_ascii=False)
            elif name in _JOB_FLAGS:
                value = 1 if value else 0
            assignments.append(f"{name} = ?")  # name is from _JOB_FIELDS only
            values.append(value)
        with self.transaction() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", (*values, id))

    def jobs_list(self, chat_id: int) -> list[dict[str, Any]]:
        rows = self._read(
            "SELECT * FROM jobs WHERE chat_id = ? AND enabled = 1 ORDER BY id",
            (chat_id,),
        )
        return [row_dict(row, json_keys=_JOB_JSON, flag_keys=_JOB_FLAGS) for row in rows]

    def job_disable(self, chat_id: int, id: int) -> bool:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE jobs SET enabled = 0 WHERE chat_id = ? AND id = ? AND enabled = 1",
                (chat_id, id),
            )
            return cur.rowcount == 1
