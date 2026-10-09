"""Audit chain: intents and outcomes, verification, tamper detection and append-only triggers.

Tamper cases open the database file with a plain sqlite3 connection after
dropping the guard triggers. That is what someone with file access could do,
so the chain must catch it without relying on the triggers.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from coworker.core.types import Decision, Tier
from coworker.store import SecretStoreError, Store, StoreError
from coworker.store import audit_rows
from coworker.store import secrets as secrets_module


def _intent(store: Store, *, tool: str = "file_copy", summary: str = "src=a.txt") -> int:
    return store.audit_intent("turn-1", "owner", tool, "LOCAL_WRITE", "ALLOW", "allowed", summary, "fake")


def _attacker(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER audit_no_update")
    conn.execute("DROP TRIGGER audit_no_delete")
    conn.commit()
    return conn


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


def test_intent_and_outcome_round_trip(store: Store) -> None:
    intent = _intent(store)
    store.audit_outcome(intent, True, "ok", "copied")
    row = store.audit_tail(1)[0]
    assert row["id"] == intent
    assert row["tool"] == "file_copy"
    assert row["tier"] == "LOCAL_WRITE" and row["decision"] == "ALLOW"
    assert row["outcome_ok"] is True and row["outcome_summary"] == "copied"
    assert store.audit_verify() == (True, None)


def test_enum_tier_and_decision_are_stored_as_their_values(store: Store) -> None:
    store.audit_intent("t", "owner", "x", Tier.READ, Decision.DENY, "not_granted", "", "p")
    row = store.audit_tail(1)[0]
    assert row["tier"] == "READ" and row["decision"] == "DENY"


def test_empty_chain_verifies(store: Store) -> None:
    assert store.audit_verify() == (True, None)


def test_tail_returns_the_last_rows_oldest_first(store: Store) -> None:
    ids = [_intent(store, summary=f"n{i}") for i in range(3)]
    assert [row["id"] for row in store.audit_tail(2)] == ids[1:]


def test_outcome_is_set_once(store: Store) -> None:
    intent = _intent(store)
    store.audit_outcome(intent, False, "failed", "boom")
    with pytest.raises(StoreError):
        store.audit_outcome(intent, True, "ok", "again")
    with pytest.raises(StoreError):
        store.audit_outcome(999, True, "ok", "unknown intent")
    assert store.audit_tail(1)[0]["outcome_ok"] is False


def test_triggers_reject_update_and_delete(store: Store, store_path: Path) -> None:
    intent = _intent(store)
    store.audit_outcome(intent, True, "ok", "done")
    raw = sqlite3.connect(store_path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            raw.execute("UPDATE audit SET tool = 'other' WHERE id = ?", (intent,))
        with pytest.raises(sqlite3.DatabaseError):
            raw.execute("UPDATE audit SET outcome_summary = 'changed' WHERE id = ?", (intent,))
        with pytest.raises(sqlite3.DatabaseError):
            raw.execute("DELETE FROM audit WHERE id = ?", (intent,))
    finally:
        raw.close()
    assert store.audit_tail(1)[0]["outcome_summary"] == "done"
    assert store.audit_verify() == (True, None)


def test_tampered_intent_field_is_detected(store: Store, store_path: Path) -> None:
    first = _intent(store, summary="first")
    _intent(store, summary="second")
    attacker = _attacker(store_path)
    attacker.execute("UPDATE audit SET args_summary = 'changed' WHERE id = ?", (first,))
    attacker.commit()
    attacker.close()
    assert store.audit_verify() == (False, first)


def test_tampered_outcome_is_detected(store: Store, store_path: Path) -> None:
    intent = _intent(store)
    store.audit_outcome(intent, True, "ok", "fine")
    attacker = _attacker(store_path)
    attacker.execute("UPDATE audit SET outcome_summary = 'forged' WHERE id = ?", (intent,))
    attacker.commit()
    attacker.close()
    assert store.audit_verify() == (False, intent)


def test_recomputed_hashes_without_the_key_are_detected(store: Store, store_path: Path) -> None:
    """An attacker can recompute SHA-256 but not the HMAC tags: the tags expose the edit."""
    first = _intent(store, summary="x")
    second = _intent(store, summary="y")
    rows = {row["id"]: dict(row) for row in store._read("SELECT * FROM audit ORDER BY id")}
    forged = {**{k: rows[first][k] for k in audit_rows._CHAIN_FIELDS}, "args_summary": "evil"}
    forged_hash = audit_rows._row_hash(forged, audit_rows.GENESIS_HASH)
    second_fields = {k: rows[second][k] for k in audit_rows._CHAIN_FIELDS}
    second_hash = audit_rows._row_hash(second_fields, forged_hash)
    attacker = _attacker(store_path)
    attacker.execute("UPDATE audit SET args_summary = 'evil', hash = ? WHERE id = ?", (forged_hash, first))
    attacker.execute("UPDATE audit SET prev_hash = ?, hash = ? WHERE id = ?", (forged_hash, second_hash, second))
    attacker.commit()
    attacker.close()
    assert store.audit_verify() == (False, first)


def test_cut_tail_is_detected(store: Store, store_path: Path) -> None:
    _intent(store)
    last = _intent(store)
    attacker = _attacker(store_path)
    attacker.execute("DELETE FROM audit WHERE id = ?", (last,))
    attacker.commit()
    attacker.close()
    assert store.audit_verify() == (False, last)


def test_chain_verifies_after_reopening_and_keeps_growing(store: Store, store_path: Path) -> None:
    _intent(store)
    _intent(store)
    store.close()
    again = Store(store_path)
    try:
        assert again.audit_verify() == (True, None)
        _intent(again)
        assert again.audit_verify() == (True, None)
    finally:
        again.close()


def test_key_is_created_once_and_kept_only_in_the_keyring(store: Store, fake_keyring, isolated_home: Path) -> None:
    _intent(store)
    key = fake_keyring.entries[("Coworker", "audit_key")]
    _intent(store)
    assert fake_keyring.entries[("Coworker", "audit_key")] == key
    for path in isolated_home.rglob("*"):
        if path.is_file():
            assert key.encode() not in path.read_bytes()


def test_intent_is_refused_when_no_key_can_be_stored(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets_module, "_backend", lambda: None)
    with pytest.raises(SecretStoreError):
        _intent(store)
    assert store.audit_tail() == []


def test_args_summary_is_redacted_and_clipped(store: Store) -> None:
    secret = "sk-" + "a" * 30
    store.audit_intent("t", "owner", "web_type", "LOCAL_WRITE", "ALLOW", "allowed",
                       f"token={secret} " + "x" * 2000, "p")
    row = store.audit_tail(1)[0]
    assert secret not in row["args_summary"]
    assert len(row["args_summary"]) <= audit_rows.ARGS_SUMMARY_LIMIT


def test_orphans_are_intents_without_an_outcome_after_sixty_seconds(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock(1000.0)
    monkeypatch.setattr(audit_rows, "time", clock)
    old = _intent(store)
    clock.now = 1030.0
    recent = _intent(store)
    done = _intent(store)
    store.audit_outcome(done, True, "ok", "")
    clock.now = 1061.0
    assert store.audit_orphans() == [old]
    clock.now = 1100.0
    assert store.audit_orphans() == [old, recent]


class _ReadFails:
    """A keyring whose reads fail, as a locked credential store does. Writes still reach the real entries."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def get_password(self, service: str, username: str) -> str:
        raise RuntimeError("the credential store is locked")

    def set_password(self, service: str, username: str, password: str) -> None:
        self._inner.set_password(service, username, password)

    def delete_password(self, service: str, username: str) -> None:
        self._inner.delete_password(service, username)


def test_an_intent_without_an_outcome_verifies(store: Store) -> None:
    _intent(store)
    _intent(store)
    assert store.audit_verify() == (True, None)


def test_removing_an_outcome_is_detected(store: Store, store_path: Path) -> None:
    """A recorded failure must not turn into an unknown: the removal breaks the chain at that row."""
    intent = _intent(store)
    store.audit_outcome(intent, False, "failed", "boom")
    attacker = _attacker(store_path)
    attacker.execute(
        "UPDATE audit SET outcome_ts = NULL, outcome_ok = NULL, outcome_code = NULL,"
        " outcome_summary = NULL, outcome_tag = NULL WHERE id = ?",
        (intent,),
    )
    attacker.commit()
    attacker.close()
    assert store.audit_verify() == (False, intent)


def test_removing_an_outcome_and_keeping_its_tag_is_detected(store: Store, store_path: Path) -> None:
    intent = _intent(store)
    store.audit_outcome(intent, True, "ok", "done")
    attacker = _attacker(store_path)
    attacker.execute(
        "UPDATE audit SET outcome_ts = NULL, outcome_ok = NULL, outcome_code = NULL,"
        " outcome_summary = NULL WHERE id = ?",
        (intent,),
    )
    attacker.commit()
    attacker.close()
    assert store.audit_verify() == (False, intent)


def test_wiping_every_row_and_the_head_is_detected_while_the_key_remains(store: Store, store_path: Path) -> None:
    """The key in the keyring was minted by the first write, so a chain that once existed cannot read as empty."""
    intent = _intent(store)
    store.audit_outcome(intent, True, "ok", "done")
    attacker = _attacker(store_path)
    attacker.execute("DELETE FROM audit")
    attacker.execute("DELETE FROM kv WHERE key = ?", (audit_rows.HEAD_KEY,))
    attacker.commit()
    attacker.close()
    assert store.audit_verify() == (False, None)


def test_a_failed_keyring_read_refuses_the_write_and_keeps_the_key(
    store_path: Path, fake_keyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = Store(store_path)
    _intent(first)
    first.close()
    key = fake_keyring.entries[("Coworker", "audit_key")]
    monkeypatch.setattr(secrets_module, "_backend", lambda: _ReadFails(fake_keyring))
    reopened = Store(store_path)
    try:
        with pytest.raises(SecretStoreError):
            _intent(reopened)
        assert fake_keyring.entries[("Coworker", "audit_key")] == key
        assert reopened.audit_verify() == (False, None)
    finally:
        reopened.close()
    monkeypatch.setattr(secrets_module, "_backend", lambda: fake_keyring)
    again = Store(store_path)
    try:
        assert again.audit_verify() == (True, None)
    finally:
        again.close()


def test_a_missing_key_on_an_existing_chain_is_reported_not_replaced(store_path: Path, fake_keyring) -> None:
    first = Store(store_path)
    _intent(first)
    first.close()
    del fake_keyring.entries[("Coworker", "audit_key")]
    reopened = Store(store_path)
    try:
        with pytest.raises(SecretStoreError):
            _intent(reopened)
        assert ("Coworker", "audit_key") not in fake_keyring.entries
        assert reopened.audit_verify() == (False, None)
    finally:
        reopened.close()
