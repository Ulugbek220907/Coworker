"""Notes the owner asked to keep, and the to-do list.

Notes are searchable through FTS5 when the database has it. Tasks are closed,
never removed: a finished task keeps its row so the history still answers
"what did I finish last week".
"""
from __future__ import annotations

import time
from typing import Any

from .base import StoreBase, row_dict
from .search import contains_all, fts_query, tokens


class NoteRows(StoreBase):
    # ------------------------------------------------------------------ notes

    def note_add(self, chat_id: int, title: str, body: str) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO notes (chat_id, title, body, ts) VALUES (?, ?, ?, ?)",
                (chat_id, title, body, time.time()),
            )
            return int(cur.lastrowid or 0)

    def notes_search(self, chat_id: int, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Notes that contain every word of ``query``."""
        if self.fts_enabled:
            match = fts_query(query)
            if match is None:
                return []
            rows = self._read(
                "SELECT n.id, n.title, n.body, n.ts"
                " FROM notes_fts JOIN notes n ON n.id = notes_fts.rowid"
                " WHERE notes_fts MATCH ? AND n.chat_id = ?"
                " ORDER BY notes_fts.rank LIMIT ?",
                (match, chat_id, limit),
            )
            return [dict(row) for row in rows]
        terms = tokens(query)
        if not terms:
            return []
        rows = self._read(
            "SELECT id, title, body, ts FROM notes WHERE chat_id = ? ORDER BY id DESC",
            (chat_id,),
        )
        hits = [dict(row) for row in rows if contains_all(f"{row['title']} {row['body']}", terms)]
        return hits[:limit]

    def notes_list(self, chat_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """The newest notes first."""
        rows = self._read(
            "SELECT id, title, body, ts FROM notes WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ tasks

    def task_add(self, chat_id: int, text: str, due_ts: float | None = None) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO tasks (chat_id, text, due_ts, done, created_at) VALUES (?, ?, ?, 0, ?)",
                (chat_id, text, due_ts, time.time()),
            )
            return int(cur.lastrowid or 0)

    def tasks_list(self, chat_id: int, open_only: bool = True) -> list[dict[str, Any]]:
        """Tasks ordered by due date (undated last), then by creation."""
        where = "AND done = 0" if open_only else ""
        rows = self._read(
            "SELECT id, text, due_ts, done, created_at FROM tasks"
            f" WHERE chat_id = ? {where}"
            " ORDER BY (due_ts IS NULL), due_ts, id",
            (chat_id,),
        )
        return [row_dict(row, flag_keys=("done",)) for row in rows]

    def task_done(self, chat_id: int, id: int) -> bool:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE tasks SET done = 1 WHERE chat_id = ? AND id = ? AND done = 0",
                (chat_id, id),
            )
            return cur.rowcount == 1
