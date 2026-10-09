"""Per-chat memory: turns, the rolling summary, durable facts and deliveries.

Rows are never removed. "Clearing" a chat or "forgetting" a fact marks rows
hidden, and every read skips them. Removing rows is a separate, explicitly
confirmed purge, which is why no method here issues DELETE.
"""
from __future__ import annotations

import time
from typing import Any

from .base import StoreBase, row_dict
from .search import contains_all, fts_query, tokens


class ChatRows(StoreBase):
    # ------------------------------------------------------------------ turns

    def turn_add(self, chat_id: int, role: str, content: str) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO turns (chat_id, role, content, ts) VALUES (?, ?, ?, ?)",
                (chat_id, role, content, time.time()),
            )
            return int(cur.lastrowid or 0)

    def turns_recent(self, chat_id: int, limit: int = 14) -> list[dict[str, Any]]:
        """The last ``limit`` turns, oldest first: the order a model reads a transcript in."""
        rows = self._read(
            "SELECT id, role, content, ts FROM turns"
            " WHERE chat_id = ? AND hidden = 0 ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        return [dict(row) for row in reversed(rows)]

    def turns_clear(self, chat_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE turns SET hidden = 1 WHERE chat_id = ? AND hidden = 0", (chat_id,))

    # ---------------------------------------------------------------- summary

    def summary_get(self, chat_id: int) -> str:
        row = self._one("SELECT text FROM summaries WHERE chat_id = ?", (chat_id,))
        return row["text"] if row else ""

    def summary_set(self, chat_id: int, text: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO summaries (chat_id, text, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (chat_id) DO UPDATE SET text = excluded.text, updated_at = excluded.updated_at",
                (chat_id, text, time.time()),
            )

    # ------------------------------------------------------------------ facts

    def fact_add(self, chat_id: int, text: str, *, kind: str = "note", untrusted: bool = False) -> int:
        """Store a durable fact. ``untrusted`` marks text that came from content, not the owner."""
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO facts (chat_id, text, kind, untrusted, ts) VALUES (?, ?, ?, ?, ?)",
                (chat_id, text, kind, 1 if untrusted else 0, time.time()),
            )
            return int(cur.lastrowid or 0)

    def facts_list(self, chat_id: int, limit: int = 60) -> list[dict[str, Any]]:
        """The newest facts first."""
        rows = self._read(
            "SELECT id, text, kind, untrusted, ts FROM facts"
            " WHERE chat_id = ? AND hidden = 0 ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        return [row_dict(row, flag_keys=("untrusted",)) for row in rows]

    def facts_search(self, chat_id: int, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Facts that contain every word of ``query``, best FTS match first when FTS is on."""
        if self.fts_enabled:
            match = fts_query(query)
            if match is None:
                return []
            rows = self._read(
                "SELECT f.id, f.text, f.kind, f.untrusted, f.ts"
                " FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid"
                " WHERE facts_fts MATCH ? AND f.chat_id = ? AND f.hidden = 0"
                " ORDER BY facts_fts.rank LIMIT ?",
                (match, chat_id, limit),
            )
            return [row_dict(row, flag_keys=("untrusted",)) for row in rows]
        terms = tokens(query)
        if not terms:
            return []
        rows = self._read(
            "SELECT id, text, kind, untrusted, ts FROM facts"
            " WHERE chat_id = ? AND hidden = 0 ORDER BY id DESC",
            (chat_id,),
        )
        hits = [row_dict(row, flag_keys=("untrusted",)) for row in rows if contains_all(row["text"], terms)]
        return hits[:limit]

    def fact_forget(self, chat_id: int, needle: str) -> int:
        """Hide every fact whose text contains ``needle``. Returns how many were hidden."""
        wanted = needle.casefold().strip()
        if not wanted:
            return 0  # an empty needle would match every fact
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT id, text FROM facts WHERE chat_id = ? AND hidden = 0", (chat_id,)
            ).fetchall()
            ids = [(row["id"],) for row in rows if wanted in row["text"].casefold()]
            conn.executemany("UPDATE facts SET hidden = 1 WHERE id = ?", ids)
            return len(ids)

    # ------------------------------------------------------------- deliveries

    def delivered_add(self, chat_id: int, path: str, name: str) -> None:
        """Record a file sent to the owner. A path is listed once: the newest send wins."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE delivered SET hidden = 1 WHERE chat_id = ? AND path = ? AND hidden = 0",
                (chat_id, path),
            )
            conn.execute(
                "INSERT INTO delivered (chat_id, path, name, ts) VALUES (?, ?, ?, ?)",
                (chat_id, path, name, time.time()),
            )

    def delivered_recent(self, chat_id: int, limit: int = 25) -> list[dict[str, Any]]:
        """The newest deliveries first."""
        rows = self._read(
            "SELECT id, path, name, ts FROM delivered"
            " WHERE chat_id = ? AND hidden = 0 ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        return [dict(row) for row in rows]
