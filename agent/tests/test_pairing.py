"""Pairing: one code, five tries per code, a global lockout that survives restarts, and a pinned owner."""
from __future__ import annotations

import hmac
from pathlib import Path

import pytest

from coworker.safety import pairing as pairing_module
from coworker.safety.pairing import (
    CODE_KEY,
    CODE_TTL_S,
    FAILURES_KEY,
    GLOBAL_FAILURE_LIMIT,
    GLOBAL_WINDOW_S,
    MAX_ATTEMPTS_PER_CODE,
    MSG_LOCKED,
    MSG_WRONG,
    Pairing,
)
from coworker.store import Store

PRIVATE = "private"


def _wrong(code: str) -> str:
    return code[:-1] + str((int(code[-1]) + 1) % 10)


def _fill_lockout(pairing: Pairing, now: float) -> None:
    """Exactly GLOBAL_FAILURE_LIMIT wrong guesses, spread over codes of five tries each."""
    for _ in range(GLOBAL_FAILURE_LIMIT // MAX_ATTEMPTS_PER_CODE):
        code = pairing.issue_code(now=now)
        for _ in range(MAX_ATTEMPTS_PER_CODE):
            pairing.redeem(_wrong(code), 7, 7, PRIVATE, now=now)


def test_code_is_eight_digits_and_stored_only_as_a_salted_hash(store: Store, store_path: Path) -> None:
    code = Pairing(store).issue_code(now=1000.0)
    assert len(code) == 8 and code.isdigit()
    record = store.kv_get(CODE_KEY)
    assert set(record) == {"salt", "hash", "expires", "attempts"}
    assert record["expires"] == 1000.0 + CODE_TTL_S
    assert code not in str(record)
    assert code.encode() not in store_path.read_bytes()


def test_owner_is_none_before_pairing(store: Store) -> None:
    assert Pairing(store).owner() is None


def test_correct_code_pins_the_owner(store: Store) -> None:
    pairing = Pairing(store)
    code = pairing.issue_code(now=1000.0)
    paired, message = pairing.redeem(code, from_id=555, chat_id=555, chat_type=PRIVATE, now=1001.0)
    assert paired is True and message
    assert pairing.owner() == (555, 555)


def test_a_code_is_single_use_and_the_owner_cannot_be_rebound(store: Store) -> None:
    pairing = Pairing(store)
    code = pairing.issue_code(now=1000.0)
    assert pairing.redeem(code, 555, 555, PRIVATE, now=1001.0)[0] is True
    paired, _ = pairing.redeem(code, 666, 666, PRIVATE, now=1002.0)
    assert paired is False
    assert pairing.owner() == (555, 555)


def test_group_chats_cannot_pair_and_do_not_count_as_failures(store: Store) -> None:
    pairing = Pairing(store)
    code = pairing.issue_code(now=1000.0)
    paired, _ = pairing.redeem(code, 1, -100, "group", now=1001.0)
    assert paired is False
    assert pairing.owner() is None
    assert store.kv_get(FAILURES_KEY) is None
    assert pairing.redeem(code, 1, 1, PRIVATE, now=1002.0)[0] is True


def test_five_wrong_attempts_burn_the_code(store: Store) -> None:
    pairing = Pairing(store)
    code = pairing.issue_code(now=0.0)
    for _ in range(MAX_ATTEMPTS_PER_CODE):
        paired, message = pairing.redeem(_wrong(code), 7, 7, PRIVATE, now=1.0)
        assert paired is False and message == MSG_WRONG
    paired, _ = pairing.redeem(code, 7, 7, PRIVATE, now=2.0)  # right code, but no tries left
    assert paired is False
    assert pairing.owner() is None


def test_expired_code_is_refused(store: Store) -> None:
    pairing = Pairing(store)
    code = pairing.issue_code(now=0.0)
    assert pairing.redeem(code, 7, 7, PRIVATE, now=CODE_TTL_S + 1)[0] is False
    assert pairing.owner() is None


def test_a_new_code_replaces_the_old_one_and_resets_attempts(store: Store) -> None:
    pairing = Pairing(store)
    old = pairing.issue_code(now=0.0)
    for _ in range(MAX_ATTEMPTS_PER_CODE):
        pairing.redeem(_wrong(old), 7, 7, PRIVATE, now=1.0)
    fresh = pairing.issue_code(now=2.0)
    assert pairing.redeem(fresh, 7, 7, PRIVATE, now=3.0)[0] is True


def test_global_lockout_after_twenty_failures_in_an_hour(store: Store) -> None:
    pairing = Pairing(store)
    _fill_lockout(pairing, now=1000.0)
    code = pairing.issue_code(now=1000.0)
    paired, message = pairing.redeem(code, 7, 7, PRIVATE, now=1001.0)
    assert paired is False and message == MSG_LOCKED
    assert pairing.owner() is None


def test_lockout_ends_when_the_failures_age_out(store: Store) -> None:
    pairing = Pairing(store)
    _fill_lockout(pairing, now=1000.0)
    later = 1000.0 + GLOBAL_WINDOW_S + 1
    code = pairing.issue_code(now=later)
    assert pairing.redeem(code, 7, 7, PRIVATE, now=later + 1)[0] is True


def test_global_lockout_survives_a_restart(store_path: Path) -> None:
    db = Store(store_path)
    _fill_lockout(Pairing(db), now=1000.0)
    db.close()
    again = Store(store_path)
    try:
        pairing = Pairing(again)
        code = pairing.issue_code(now=1000.0)
        paired, message = pairing.redeem(code, 7, 7, PRIVATE, now=1001.0)
        assert paired is False and message == MSG_LOCKED
    finally:
        again.close()


def test_code_comparison_goes_through_compare_digest(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    pairing = Pairing(store)
    code = pairing.issue_code(now=0.0)
    pairing.redeem(_wrong(code), 7, 7, PRIVATE, now=1.0)
    assert calls, "the code hash must be compared with hmac.compare_digest"


def test_issuing_a_code_at_startup_does_not_clear_the_lockout(store: Store) -> None:
    """A restart re-issues a code, so issuing one must never be the thing that reopens redemption."""
    pairing = Pairing(store)
    _fill_lockout(pairing, now=1000.0)
    code = pairing.issue_code(now=1001.0)
    paired, message = pairing.redeem(code, 7, 7, PRIVATE, now=1002.0)
    assert paired is False and message == MSG_LOCKED


def test_a_local_clear_lets_the_owner_pair_after_a_lockout(store: Store) -> None:
    """A stranger can hold the lockout closed by re-arming it; the desktop must have a way out."""
    pairing = Pairing(store)
    _fill_lockout(pairing, now=1000.0)
    pairing.clear_lockout()
    assert store.kv_get(FAILURES_KEY, None) == []
    code = pairing.issue_code(now=1001.0)
    assert pairing.redeem(code, 555, 555, PRIVATE, now=1002.0)[0] is True
    assert pairing.owner() == (555, 555)


def test_code_hash_uses_salted_pbkdf2(store: Store) -> None:
    pairing = Pairing(store)
    pairing.issue_code(now=0.0)
    record = store.kv_get(CODE_KEY)
    assert len(bytes.fromhex(record["salt"])) == 16
    assert len(record["hash"]) == 64
    assert pairing_module.PBKDF2_ITERATIONS >= 100_000
