"""Approval rows: frozen tool calls waiting for the owner's tap.

A pending approval is the only way a CONFIRM turns into an action. Its status
moves through a fixed path: pending to approved, denied, expired or cancelled,
and approved to done. Every move is a compare-and-set inside one IMMEDIATE
transaction, so two taps, a tap racing a stop, or a tap after expiry cannot all
succeed.

The id and nonce are short because they travel in Telegram callback data, which
is limited to 64 bytes and is kept at 24 here. The nonce is what a tap must
present; the id only finds the row.
"""
from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .base import KILL_GENERATION_KEY, StoreBase, StoreError

ID_LEN = 10
NONCE_LEN = 8
PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
EXPIRED = "expired"
CANCELLED = "cancelled"
DONE = "done"

_CREATE_ATTEMPTS = 5


@dataclass(frozen=True)
class Approval:
    id: str
    nonce: str
    chat_id: int
    tool: str
    args: dict[str, Any]
    summary: str
    provenance: int
    autonomy: str
    generation: int
    status: str
    two_channel: bool
    local_ok: bool
    created_at: float
    expires_at: float


def _from_row(row: sqlite3.Row) -> Approval:
    return Approval(
        id=row["id"],
        nonce=row["nonce"],
        chat_id=row["chat_id"],
        tool=row["tool"],
        args=json.loads(row["args"]),
        summary=row["summary"],
        provenance=row["provenance"],
        autonomy=row["autonomy"],
        generation=row["generation"],
        status=row["status"],
        two_channel=bool(row["two_channel"]),
        local_ok=bool(row["local_ok"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def _current_generation(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (KILL_GENERATION_KEY,)).fetchone()
    return int(json.loads(row["value"])) if row else 0


def _same_text(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class ApprovalRows(StoreBase):
    def approval_create(
        self,
        chat_id: int,
        tool: str,
        args: dict[str, Any],
        summary: str,
        provenance: int,
        autonomy: str,
        generation: int,
        two_channel: bool,
        ttl_s: float,
    ) -> Approval:
        now = time.time()
        payload = json.dumps(args, ensure_ascii=False, sort_keys=True)
        for _ in range(_CREATE_ATTEMPTS):
            approval_id = secrets.token_hex(ID_LEN // 2)
            nonce = secrets.token_hex(NONCE_LEN // 2)
            try:
                with self.transaction() as conn:
                    conn.execute(
                        "INSERT INTO approvals (id, nonce, chat_id, tool, args, summary, provenance,"
                        " autonomy, generation, status, two_channel, local_ok, created_at, expires_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                        (approval_id, nonce, chat_id, tool, payload, summary, int(provenance), autonomy,
                         generation, PENDING, 1 if two_channel else 0, now, now + ttl_s),
                    )
            except sqlite3.IntegrityError:
                continue  # an id collision: the next draw is fresh
            return Approval(
                id=approval_id, nonce=nonce, chat_id=chat_id, tool=tool, args=dict(args), summary=summary,
                provenance=int(provenance), autonomy=autonomy, generation=generation, status=PENDING,
                two_channel=bool(two_channel), local_ok=False, created_at=now, expires_at=now + ttl_s,
            )
        raise StoreError("no free approval id after several draws")

    def approval_get(self, approval_id: str) -> Approval | None:
        row = self._one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        return _from_row(row) if row else None

    def _claimable(
        self,
        conn: sqlite3.Connection,
        approval_id: str,
        nonce: str,
        actor_id: int,
        owner_id: int | None,
    ) -> sqlite3.Row | None:
        """The pending row, when the tap is the owner's and carries the right nonce."""
        if owner_id is None or actor_id != owner_id:
            return None
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None or row["status"] != PENDING or not _same_text(row["nonce"], nonce):
            return None
        return row

    def approval_consume(
        self,
        approval_id: str,
        nonce: str,
        actor_id: int,
        owner_id: int | None,
        *,
        now: float | None = None,
    ) -> Approval | None:
        """Approve a pending call: one atomic step, or None with nothing changed.

        Refused when the tap is not the owner's, the nonce is wrong, the approval
        is not pending, a two-channel approval has no local OK, or the kill-switch
        generation has moved on. An expired approval is marked expired.
        """
        moment = time.time() if now is None else now
        with self.transaction() as conn:
            row = self._claimable(conn, approval_id, nonce, actor_id, owner_id)
            if row is None:
                return None
            if row["expires_at"] <= moment:
                conn.execute("UPDATE approvals SET status = ? WHERE id = ? AND status = ?",
                             (EXPIRED, approval_id, PENDING))
                return None
            if row["two_channel"] and not row["local_ok"]:
                return None
            if row["generation"] != _current_generation(conn):
                return None
            cur = conn.execute(
                "UPDATE approvals SET status = ? WHERE id = ? AND status = ?",
                (APPROVED, approval_id, PENDING),
            )
            if cur.rowcount != 1:
                return None
            return _from_row(conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone())

    def approval_decline(
        self,
        approval_id: str,
        nonce: str,
        actor_id: int,
        owner_id: int | None,
        *,
        now: float | None = None,
    ) -> bool:
        """The owner's "no": the call is denied and can never be approved afterwards."""
        moment = time.time() if now is None else now
        with self.transaction() as conn:
            row = self._claimable(conn, approval_id, nonce, actor_id, owner_id)
            if row is None or row["expires_at"] <= moment:
                return False
            cur = conn.execute(
                "UPDATE approvals SET status = ? WHERE id = ? AND status = ?",
                (DENIED, approval_id, PENDING),
            )
            return cur.rowcount == 1

    def approval_local_approve(self, approval_id: str) -> bool:
        """The desktop side of a two-channel approval. Only a pending, unexpired one counts."""
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE approvals SET local_ok = 1"
                " WHERE id = ? AND status = ? AND two_channel = 1 AND expires_at > ?",
                (approval_id, PENDING, time.time()),
            )
            return cur.rowcount == 1

    def approval_done(self, approval_id: str) -> bool:
        """Mark an approved call as run. Only an approved row can move to done."""
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status = ? WHERE id = ? AND status = ?",
                (DONE, approval_id, APPROVED),
            )
            return cur.rowcount == 1

    def approval_cancel_pending(self, reason: str) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status = ?, note = ? WHERE status = ?",
                (CANCELLED, reason, PENDING),
            )
            return cur.rowcount

    def approvals_expire(self, now: float | None = None) -> int:
        moment = time.time() if now is None else now
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status = ? WHERE status = ? AND expires_at <= ?",
                (EXPIRED, PENDING, moment),
            )
            return cur.rowcount

    def approvals_pending(self, chat_id: int) -> list[Approval]:
        rows = self._read(
            "SELECT * FROM approvals WHERE chat_id = ? AND status = ? ORDER BY created_at, id",
            (chat_id, PENDING),
        )
        return [_from_row(row) for row in rows]
