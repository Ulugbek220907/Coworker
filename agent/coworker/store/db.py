"""The store facade: the one class every Coworker module uses.

``Store`` composes one mixin per part of the schema. Each mixin owns its SQL, so
no module outside ``store/`` writes SQL and the rules for each table sit next to
its queries. Construct it with a file path; the schema is created or brought up
to date on open.
"""
from __future__ import annotations

import json
from typing import Any

from .approval_rows import ApprovalRows
from .audit_rows import AuditRows
from .chat_rows import ChatRows
from .note_rows import NoteRows
from .schedule_rows import ScheduleRows


class Store(ChatRows, ApprovalRows, AuditRows, ScheduleRows, NoteRows):
    """SQLite in WAL mode with foreign keys on and a 5 second busy timeout.

    Each thread gets its own connection; writes are serialised through one lock.
    ``use_fts=False`` makes facts and notes search by scanning even when FTS5 is
    available, which the tests use to exercise the fallback.
    """

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return default if row is None else json.loads(row["value"])

    def kv_set(self, key: str, value: Any) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def kv_increment(self, key: str, by: int = 1) -> int:
        """Add ``by`` to an integer value in one transaction, so concurrent bumps all count."""
        with self.transaction() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            new = (int(json.loads(row["value"])) if row else 0) + by
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                (key, json.dumps(new)),
            )
            return new
