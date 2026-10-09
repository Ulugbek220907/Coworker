"""Kill switch: stop cancels work and approvals and moves the generation; panic persists; resume is local."""
from __future__ import annotations

import inspect
from pathlib import Path

from coworker.core.types import Autonomy, CallContext, CancelToken, Provenance, Tier, Verdict
from coworker.safety.approvals import ApprovalBroker
from coworker.safety.killswitch import KillSwitch
from coworker.store import Store
from coworker.store.approval_rows import CANCELLED
from coworker.tools.registry import ToolSpec


def _pending_approval(store: Store) -> str:
    spec = ToolSpec(name="file_copy", family="files", tier=Tier.LOCAL_WRITE, description="copy",
                    parameters={"type": "object", "properties": {}}, handler=lambda call: None)
    ctx = CallContext(turn_id="t", actor="owner", chat_id=42, autonomy=Autonomy.ASK_FOR_WRITES,
                      grants=frozenset({"files"}), generation=store.kv_get("kill_generation", 0),
                      provenance=Provenance.OWNER)
    return ApprovalBroker(store).propose(42, spec, {"src": "a"}, Verdict.confirm("x"), ctx).id


def test_stop_cancels_every_registered_token(store: Store) -> None:
    kill = KillSwitch(store)
    tokens = [CancelToken() for _ in range(3)]
    for token in tokens:
        kill.register(token)
    kill.stop()
    assert all(token.cancelled for token in tokens)


def test_unregistered_tokens_are_not_cancelled(store: Store) -> None:
    kill = KillSwitch(store)
    finished, running = CancelToken(), CancelToken()
    kill.register(finished)
    kill.register(running)
    kill.unregister(finished)
    kill.unregister(CancelToken())  # unknown token: no error
    kill.stop()
    assert running.cancelled is True
    assert finished.cancelled is False


def test_stop_moves_the_generation_and_drops_queued_work(store: Store) -> None:
    kill = KillSwitch(store)
    before = kill.generation
    assert kill.is_current(before)
    kill.stop()
    assert kill.generation == before + 1
    assert not kill.is_current(before)
    assert kill.is_current(before + 1)


def test_stop_cancels_pending_approvals(store: Store) -> None:
    approval_id = _pending_approval(store)
    KillSwitch(store).stop()
    approval = store.approval_get(approval_id)
    assert approval is not None and approval.status == CANCELLED


def test_generation_survives_reopening(store_path: Path) -> None:
    db = Store(store_path)
    KillSwitch(db).stop()
    KillSwitch(db).stop()
    db.close()
    again = Store(store_path)
    try:
        assert KillSwitch(again).generation == 2
    finally:
        again.close()


def test_panic_stops_work_and_sets_the_flag(store: Store) -> None:
    kill = KillSwitch(store)
    token = CancelToken()
    kill.register(token)
    before = kill.generation
    kill.panic()
    assert kill.is_panic() is True
    assert token.cancelled is True
    assert kill.generation == before + 1


def test_panic_survives_a_restart(store_path: Path) -> None:
    db = Store(store_path)
    KillSwitch(db).panic()
    db.close()
    again = Store(store_path)
    try:
        assert KillSwitch(again).is_panic() is True
    finally:
        again.close()


def test_resume_local_clears_panic_and_only_reports_a_change(store: Store) -> None:
    kill = KillSwitch(store)
    assert kill.resume_local() is False  # nothing to resume
    kill.panic()
    assert kill.resume_local() is True
    assert kill.is_panic() is False
    assert kill.resume_local() is False


def test_resume_local_takes_no_arguments_so_no_chat_text_can_reach_it() -> None:
    assert list(inspect.signature(KillSwitch.resume_local).parameters) == ["self"]
