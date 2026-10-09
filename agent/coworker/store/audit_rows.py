"""Append-only audit chain: every decision and its outcome, tamper-evident.

The audit log is the owner's only record of what the agent did while they were
away. A row that can be edited quietly is worse than no row, so four things make
an edit visible:

* triggers refuse UPDATE and DELETE, except one write that sets a row's outcome;
* each row's hash covers its fields and the previous row's hash, so rows cannot
  be reordered or dropped from the middle;
* each hash carries an HMAC-SHA256 tag under a key kept in the OS keyring, so
  someone who edits the file cannot recompute valid tags;
* the newest row is anchored in kv with its own tag, so cutting the tail shows.

Outcomes are not part of the row hash: they are written after the intent, and
changing the hash would break the chain behind them. Each row instead carries an
outcome tag. While the outcome is pending the tag covers that pending state, so
a row whose outcome has been cleared no longer matches the tag it was written
with. Once set, the tag covers the outcome's values.

A wipe is caught by the keyring as well as the file. The first write mints the
audit key in the keyring, and the key outlives the rows. A key with no rows and
no head means the chain was removed. The key is minted inside the store's write
transaction, so two writers cannot each mint one, and an existing chain whose
key has gone is refused rather than given a new key.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from enum import Enum
from typing import Any

from ..policy.prohibited import redact
from .base import StoreBase, StoreError
from .secrets import SecretStoreError, keyring_has_entry, read_secret, set_secret

GENESIS_HASH = "0" * 64
ARGS_SUMMARY_LIMIT = 512
ORPHAN_AGE_S = 60
HEAD_KEY = "audit_head"
AUDIT_KEY_NAME = "audit_key"

_CHAIN_FIELDS = ("id", "turn_id", "actor", "tool", "tier", "decision", "code", "args_summary", "provider", "ts")


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _row_hash(fields: dict[str, Any], prev_hash: str) -> str:
    return hashlib.sha256(_canonical({**fields, "prev_hash": prev_hash}).encode("utf-8")).hexdigest()


def _tag(key: bytes, text: str) -> str:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def _pending_text(row_hash: str) -> str:
    """What a row's outcome tag covers while no outcome has been written."""
    return _canonical(["pending", row_hash])


def _outcome_text(intent_id: int, ok: int, code: Any, summary: Any, ts: float) -> str:
    return _canonical([intent_id, ok, code, summary, ts])


def _head_text(row_id: int, row_hash: str) -> str:
    return _canonical(["head", row_id, row_hash])


def _same(a: Any, b: Any) -> bool:
    return hmac.compare_digest(str(a or "").encode("utf-8"), str(b or "").encode("utf-8"))


def _plain(value: Any) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def _clip(text: str) -> str:
    return redact(str(text or ""))[:ARGS_SUMMARY_LIMIT]


class AuditRows(StoreBase):
    def _verifying_key(self) -> bytes | None:
        """The HMAC key if one exists. Reading never creates a key.

        Raises SecretStoreError when the keyring cannot answer: an unanswered read
        is not a missing key, and treating it as one would mint a replacement.
        """
        if self._audit_secret is None:
            value = read_secret(AUDIT_KEY_NAME)
            if value is None:
                return None
            self._audit_secret = value
        return self._audit_secret.encode("utf-8")

    def _signing_key(self) -> bytes:
        """The HMAC key for writing. Must be called inside ``transaction()``.

        A key is minted only for an empty store. A chain whose key has gone is
        refused, not re-keyed: every row it holds would become unverifiable.
        """
        key = self._verifying_key()
        if key is not None:
            return key
        if self._chain_started():
            raise SecretStoreError("the audit key is missing, but the audit chain has rows; it will not be replaced")
        value = secrets.token_hex(32)
        set_secret(AUDIT_KEY_NAME, value)
        self._audit_secret = value
        return value.encode("utf-8")

    def _chain_started(self) -> bool:
        if self._one("SELECT 1 FROM audit LIMIT 1") is not None:
            return True
        return self._one("SELECT 1 FROM kv WHERE key = ?", (HEAD_KEY,)) is not None

    def audit_intent(
        self,
        turn_id: str,
        actor: str,
        tool: str,
        tier: Any,
        decision: Any,
        code: str,
        args_summary: str,
        provider: str,
    ) -> int:
        """Record that a call is about to run. Returns the intent id for ``audit_outcome``.

        Raises SecretStoreError when the audit key cannot be read, or when no key
        exists and the chain already has rows, or when no key can be stored: an
        action must not run unrecorded, so the caller refuses it.
        """
        now = time.time()
        with self.transaction() as conn:
            key = self._signing_key()
            last = conn.execute("SELECT id, hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            row_id = (last["id"] + 1) if last else 1
            prev = last["hash"] if last else GENESIS_HASH
            fields = {
                "id": row_id,
                "turn_id": turn_id,
                "actor": actor,
                "tool": tool,
                "tier": _plain(tier),
                "decision": _plain(decision),
                "code": code,
                "args_summary": _clip(args_summary),
                "provider": provider,
                "ts": now,
            }
            row_hash = _row_hash(fields, prev)
            conn.execute(
                "INSERT INTO audit (id, turn_id, actor, tool, tier, decision, code, args_summary, provider,"
                " ts, prev_hash, hash, tag, outcome_tag) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row_id, turn_id, actor, tool, fields["tier"], fields["decision"], code, fields["args_summary"],
                 provider, now, prev, row_hash, _tag(key, row_hash), _tag(key, _pending_text(row_hash))),
            )
            head = {"id": row_id, "hash": row_hash, "tag": _tag(key, _head_text(row_id, row_hash))}
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                (HEAD_KEY, json.dumps(head)),
            )
        return row_id

    def audit_outcome(self, intent_id: int, ok: bool, code: str, summary: str) -> None:
        """Attach the result to an intent. Allowed once per intent; a second call raises.

        An intent always has a key, because ``audit_intent`` minted it. A missing
        key here means the chain was damaged, so the outcome is refused rather
        than signed with a new key.
        """
        ok_flag = 1 if ok else 0
        clean_summary = _clip(summary)
        ts = time.time()
        with self.transaction() as conn:
            key = self._verifying_key()
            if key is None:
                raise SecretStoreError("the audit key is missing, so the outcome cannot be recorded")
            tag = _tag(key, _outcome_text(intent_id, ok_flag, code, clean_summary, ts))
            cur = conn.execute(
                "UPDATE audit SET outcome_ok = ?, outcome_code = ?, outcome_summary = ?,"
                " outcome_ts = ?, outcome_tag = ? WHERE id = ? AND outcome_ts IS NULL",
                (ok_flag, code, clean_summary, ts, tag, intent_id),
            )
            if cur.rowcount != 1:
                raise StoreError(f"audit intent {intent_id} is unknown or already has an outcome")

    def audit_tail(self, limit: int = 20) -> list[dict[str, Any]]:
        """The last ``limit`` rows, oldest first."""
        rows = self._read("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for row in reversed(rows):
            item = dict(row)
            if item["outcome_ok"] is not None:
                item["outcome_ok"] = bool(item["outcome_ok"])
            out.append(item)
        return out

    def audit_verify(self) -> tuple[bool, int | None]:
        """Check the whole chain. Returns (True, None), or (False, id of the first bad row).

        A missing key with rows present is a failure, not an error: the rows
        cannot be authenticated, so they must not be trusted. A keyring that
        cannot answer also fails, for the same reason.
        """
        try:
            return self._verify_chain()
        except SecretStoreError:
            return False, None

    def _verify_chain(self) -> tuple[bool, int | None]:
        rows = self._read("SELECT * FROM audit ORDER BY id")
        head_row = self._one("SELECT value FROM kv WHERE key = ?", (HEAD_KEY,))
        key = self._verifying_key()
        if key is None:
            return (not rows and head_row is None), None
        prev = GENESIS_HASH
        for row in rows:
            if not _row_ok(row, prev, key):
                return False, row["id"]
            prev = row["hash"]
        if head_row is None:
            # Rows without a head mean the anchor was cut. No rows and no head is an
            # empty chain only if the keyring never received its key. A key supplied
            # only by the environment cannot show that, so a wipe is not detectable
            # on such a machine. A crash between minting the key and the first commit
            # leaves the same state; the next successful write clears it.
            return (not rows and not keyring_has_entry(AUDIT_KEY_NAME)), None
        head = json.loads(head_row["value"])
        last_id = rows[-1]["id"] if rows else 0
        if not _same(head["tag"], _tag(key, _head_text(head["id"], head["hash"]))):
            return False, head["id"]
        if head["id"] > last_id:
            return False, last_id + 1  # the tail was cut
        if head["id"] < last_id or head["hash"] != prev:
            return False, last_id
        return True, None

    def audit_orphans(self) -> list[int]:
        """Intents with no outcome after ORPHAN_AGE_S: the call may have run and never reported."""
        cutoff = time.time() - ORPHAN_AGE_S
        rows = self._read(
            "SELECT id FROM audit WHERE outcome_ts IS NULL AND ts < ? ORDER BY id",
            (cutoff,),
        )
        return [row["id"] for row in rows]


def _row_ok(row: Any, prev: str, key: bytes) -> bool:
    if row["prev_hash"] != prev:
        return False
    row_hash = _row_hash({name: row[name] for name in _CHAIN_FIELDS}, prev)
    if not _same(row["hash"], row_hash) or not _same(row["tag"], _tag(key, row_hash)):
        return False
    if row["outcome_ts"] is None:
        # Pending: no outcome value may be present, and the tag must be the pending tag.
        if any(row[name] is not None for name in ("outcome_ok", "outcome_code", "outcome_summary")):
            return False
        return _same(row["outcome_tag"], _tag(key, _pending_text(row_hash)))
    if row["outcome_ok"] is None or row["outcome_code"] is None or row["outcome_summary"] is None:
        return False
    text = _outcome_text(row["id"], int(row["outcome_ok"]), row["outcome_code"],
                         row["outcome_summary"], row["outcome_ts"])
    return _same(row["outcome_tag"], _tag(key, text))
