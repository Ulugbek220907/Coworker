"""Approvals: frozen calls, one tap, nonce and actor checks, expiry, two channels and generations."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from coworker.core.types import Autonomy, CallContext, Provenance, Tier, Verdict
from coworker.safety.approvals import INTERACTIVE_TTL_S, UNATTENDED_TTL_S, ApprovalBroker
from coworker.store import Approval, Store
from coworker.store.approval_rows import APPROVED, CANCELLED, DENIED, DONE, EXPIRED, PENDING
from coworker.tools.registry import ToolSpec

OWNER = 42


def _spec(name: str = "file_copy") -> ToolSpec:
    return ToolSpec(
        name=name,
        family="files",
        tier=Tier.LOCAL_WRITE,
        description="copy a file",
        parameters={"type": "object", "properties": {}},
        handler=lambda call: None,
    )


def _ctx(
    *,
    actor: str = "owner",
    chat_id: int | None = OWNER,
    generation: int = 0,
    provenance: Provenance = Provenance.OWNER,
) -> CallContext:
    return CallContext(
        turn_id="turn-1",
        actor=actor,
        chat_id=chat_id,
        autonomy=Autonomy.ASK_FOR_WRITES,
        grants=frozenset({"files"}),
        generation=generation,
        provenance=provenance,
    )


def _propose(broker: ApprovalBroker, args: dict[str, Any] | None = None, *, two_channel: bool = False,
             ctx: CallContext | None = None) -> Approval:
    verdict = Verdict.confirm("tier_default", summary="copy a.txt", two_channel=two_channel)
    return broker.propose(OWNER, _spec(), args or {"src": "a.txt"}, verdict, ctx or _ctx())


def test_propose_freezes_arguments_and_context(store: Store) -> None:
    broker = ApprovalBroker(store)
    args = {"src": "C:/a.txt", "dst": "C:/b"}
    approval = broker.propose(OWNER, _spec(), args, Verdict.confirm("tier_default", summary="copy a"),
                              _ctx(provenance=Provenance.CONTENT))
    args["src"] = "changed after the card was shown"
    stored = store.approval_get(approval.id)
    assert stored is not None
    assert stored.args == {"src": "C:/a.txt", "dst": "C:/b"}
    assert stored.tool == "file_copy"
    assert stored.summary == "copy a"
    assert stored.provenance == int(Provenance.CONTENT)
    assert stored.autonomy == "ask_for_writes"
    assert stored.status == PENDING
    assert len(stored.id) == 10 and len(stored.nonce) == 8


def test_ttl_is_five_minutes_interactive_and_thirty_unattended(store: Store) -> None:
    broker = ApprovalBroker(store)
    interactive = _propose(broker)
    unattended = _propose(broker, ctx=_ctx(actor="scheduler", chat_id=None))
    assert interactive.expires_at - interactive.created_at == pytest.approx(INTERACTIVE_TTL_S)
    assert unattended.expires_at - unattended.created_at == pytest.approx(UNATTENDED_TTL_S)
    assert (INTERACTIVE_TTL_S, UNATTENDED_TTL_S) == (5 * 60, 30 * 60)


def test_buttons_fit_the_telegram_callback_limit(store: Store) -> None:
    broker = ApprovalBroker(store)
    approval = _propose(broker)
    yes, no = (button["callback_data"] for button in broker.buttons(approval)[0])
    assert yes == f"ap:{approval.id}:{approval.nonce}:y"
    assert no == f"ap:{approval.id}:{approval.nonce}:n"
    assert len(yes.encode("utf-8")) <= 24 and len(no.encode("utf-8")) <= 24
    assert ApprovalBroker.parse_callback(yes) == (approval.id, approval.nonce, True)
    assert ApprovalBroker.parse_callback(no) == (approval.id, approval.nonce, False)
    assert ApprovalBroker.parse_callback("ap:short:abc:y") is None
    assert ApprovalBroker.parse_callback("other:1:2:3") is None


def test_consume_approves_exactly_once(store: Store) -> None:
    broker = ApprovalBroker(store)
    approval = _propose(broker)
    taken = broker.consume(approval.id, approval.nonce, OWNER, OWNER)
    assert taken is not None and taken.status == APPROVED
    assert broker.consume(approval.id, approval.nonce, OWNER, OWNER) is None
    assert store.approval_get(approval.id).status == APPROVED


def test_wrong_nonce_is_refused_and_the_approval_stays_pending(store: Store) -> None:
    broker = ApprovalBroker(store)
    approval = _propose(broker)
    assert broker.consume(approval.id, "00000000", OWNER, OWNER) is None
    assert store.approval_get(approval.id).status == PENDING


def test_a_tap_from_anyone_but_the_owner_is_refused(store: Store) -> None:
    broker = ApprovalBroker(store)
    approval = _propose(broker)
    assert broker.consume(approval.id, approval.nonce, 99, OWNER) is None
    assert broker.consume(approval.id, approval.nonce, OWNER, None) is None
    assert broker.decline(approval.id, approval.nonce, 99, OWNER) is False
    assert store.approval_get(approval.id).status == PENDING


def test_expired_approval_cannot_be_consumed(store: Store) -> None:
    broker = ApprovalBroker(store)
    approval = _propose(broker)
    late = approval.expires_at + 1
    assert store.approval_consume(approval.id, approval.nonce, OWNER, OWNER, now=late) is None
    assert store.approval_get(approval.id).status == EXPIRED


def test_expiry_sweep_marks_only_overdue_pending_approvals(store: Store) -> None:
    short = store.approval_create(OWNER, "file_copy", {}, "short", 1, "ask_for_writes", 0, False, ttl_s=10)
    long = store.approval_create(OWNER, "file_copy", {}, "long", 1, "ask_for_writes", 0, False, ttl_s=1000)
    assert store.approvals_expire(now=short.expires_at + 1) == 1
    assert store.approval_get(short.id).status == EXPIRED
    assert store.approval_get(long.id).status == PENDING


def test_two_channel_approval_needs_the_local_ok(store: Store) -> None:
    broker = ApprovalBroker(store)
    approval = _propose(broker, two_channel=True)
    assert broker.consume(approval.id, approval.nonce, OWNER, OWNER) is None
    assert store.approval_get(approval.id).status == PENDING
    assert broker.local_approve(approval.id) is True
    assert broker.consume(approval.id, approval.nonce, OWNER, OWNER) is not None


def test_local_approve_applies_only_to_pending_two_channel_approvals(store: Store) -> None:
    broker = ApprovalBroker(store)
    single = _propose(broker)
    assert broker.local_approve(single.id) is False
    double = _propose(broker, two_channel=True)
    assert broker.consume(double.id, double.nonce, OWNER, OWNER) is None
    assert broker.local_approve(double.id) is True
    broker.consume(double.id, double.nonce, OWNER, OWNER)
    assert broker.local_approve(double.id) is False


def test_generation_mismatch_is_refused(store: Store) -> None:
    broker = ApprovalBroker(store)
    approval = _propose(broker, ctx=_ctx(generation=0))
    store.kv_increment("kill_generation")  # a stop moved the generation on
    assert broker.consume(approval.id, approval.nonce, OWNER, OWNER) is None
    assert store.approval_get(approval.id).status == PENDING


def test_decline_denies_the_call_for_good(store: Store) -> None:
    broker = ApprovalBroker(store)
    approval = _propose(broker)
    assert broker.decline(approval.id, approval.nonce, OWNER, OWNER) is True
    assert store.approval_get(approval.id).status == DENIED
    assert broker.consume(approval.id, approval.nonce, OWNER, OWNER) is None


def test_done_follows_approval_and_only_from_approved(store: Store) -> None:
    broker = ApprovalBroker(store)
    approval = _propose(broker)
    assert store.approval_done(approval.id) is False
    broker.consume(approval.id, approval.nonce, OWNER, OWNER)
    assert store.approval_done(approval.id) is True
    assert store.approval_get(approval.id).status == DONE


def test_cancel_pending_moves_every_pending_approval(store: Store) -> None:
    broker = ApprovalBroker(store)
    first = _propose(broker)
    second = _propose(broker)
    assert store.approval_cancel_pending("stop") == 2
    assert store.approval_get(first.id).status == CANCELLED
    assert store.approval_get(second.id).status == CANCELLED
    assert store.approvals_pending(OWNER) == []


def test_pending_approvals_survive_reopening_the_file(store_path: Path) -> None:
    db = Store(store_path)
    approval = _propose(ApprovalBroker(db), {"a": 1})
    db.close()
    again = Store(store_path)
    try:
        pending = again.approvals_pending(OWNER)
        assert [p.id for p in pending] == [approval.id]
        assert pending[0].args == {"a": 1}
        assert ApprovalBroker(again).consume(approval.id, approval.nonce, OWNER, OWNER) is not None
    finally:
        again.close()


def test_racing_taps_have_exactly_one_winner(store: Store) -> None:
    broker = ApprovalBroker(store)
    approval = _propose(broker)
    results: list[Approval | None] = []
    lock = threading.Lock()

    def tap() -> None:
        outcome = broker.consume(approval.id, approval.nonce, OWNER, OWNER)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=tap) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(result is not None for result in results) == 1
