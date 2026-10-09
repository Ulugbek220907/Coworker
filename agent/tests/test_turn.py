"""Orchestrator regressions: turn order, scheduled runs, approved runs and the text a turn keeps.

The dispatcher is a spy that records the turn it is handed, so each test can
check the provenance and owner words a turn carries. The provider is a script of
answers. The store is the real one.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from coworker.core.types import Provenance, ToolResult
from coworker.governor import budget as budget_module
from coworker.governor.budget import Budget
from coworker.llm_base import NO_ANSWER
from coworker.orchestrator import prompt
from coworker.orchestrator.turn import Orchestrator
from coworker.safety import ApprovalBroker, KillSwitch
from coworker.scheduler import JobFailed
from coworker.tools.registry import Registry, Services


def answer(text: str = "", calls=()) -> SimpleNamespace:
    return SimpleNamespace(text=text, tool_calls=list(calls))


def call(name: str, args: dict, call_id: str = "c1") -> SimpleNamespace:
    return SimpleNamespace(id=call_id, name=name, args=args)


class ScriptedProvider:
    dialect = "openai"
    name = "scripted"

    def __init__(self, *answers, pause: float = 0.0) -> None:
        self._answers = list(answers)
        self.seen: list[list] = []   # the messages of each model call, as the model received them
        self.pause = pause
        self.running = 0
        self.peak = 0

    async def chat(self, system, messages, tools, max_tokens=900):
        self.seen.append(list(messages))
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            if self.pause:
                await asyncio.sleep(self.pause)
        finally:
            self.running -= 1
        return self._answers.pop(0) if self._answers else answer("ok")


class SpyDispatcher:
    """Records every turn it is handed. ``on_execute`` runs inside an approved call."""

    def __init__(self, result: ToolResult | None = None, on_execute=None) -> None:
        self.result = result or ToolResult.fail("nope", "no")
        self.on_execute = on_execute
        self.turns: list = []

    async def invoke(self, name, args, turn):
        self.turns.append(turn)
        if self.result.ok and self.result.data.get("end_turn"):
            turn.end_turn = True
        return self.result

    async def execute_approved(self, approval, turn):
        self.turns.append(turn)
        if self.on_execute is not None:
            self.on_execute(turn)
        return ToolResult(ok=True, data={"message": "done"})


def make_orchestrator(store, provider, *, dispatcher=None, kill=None):
    kill = kill or KillSwitch(store)
    svc = Services(store=store, budget=Budget(store), approvals=ApprovalBroker(store), llm=provider, kill=kill)
    dispatcher = dispatcher or SpyDispatcher()
    orchestrator = Orchestrator(registry=Registry(), dispatcher=dispatcher, svc=svc, config={})
    return orchestrator, svc, kill


def user_texts(messages) -> list[str]:
    return [m.content for m in messages if m.role == "user"]


# ------------------------------------------------------------- owner turns

def test_two_owner_messages_do_not_run_their_model_turns_at_once(store):
    provider = ScriptedProvider(answer("first"), answer("second"), pause=0.02)
    orchestrator, _, _ = make_orchestrator(store, provider)

    async def both():
        return await asyncio.gather(
            orchestrator.handle_owner(42, "one"),
            orchestrator.handle_owner(42, "two"),
        )

    replies = asyncio.run(both())

    assert provider.peak == 1
    assert [r.text for r in replies] == ["first", "second"]


def test_an_empty_model_answer_is_not_reported_as_done(store):
    orchestrator, _, _ = make_orchestrator(store, ScriptedProvider(answer("")))

    reply = asyncio.run(orchestrator.handle_owner(42, "send me the report"))

    assert reply.text == NO_ANSWER
    assert reply.failed == "javob olinmadi"


def test_a_question_is_kept_in_history_so_the_next_turn_has_its_context(store):
    spy = SpyDispatcher(ToolResult(ok=True, data={"asked": True, "end_turn": True}))
    provider = ScriptedProvider(answer("", [call("ask", {"question": "Qaysi fayl?", "options": ["a.pdf", "b.pdf"]})]))
    orchestrator, _, _ = make_orchestrator(store, provider, dispatcher=spy)

    asyncio.run(orchestrator.handle_owner(42, "find the report"))

    last = store.turns_recent(42)[-1]
    assert last["role"] == "assistant"
    assert "Qaysi fayl?" in last["content"] and "a.pdf / b.pdf" in last["content"]


def test_a_forwarded_message_is_judged_as_content_not_as_the_owners_words(store):
    spy = SpyDispatcher()
    provider = ScriptedProvider(answer("", [call("notes_add", {"text": "x"})]), answer("ok"))
    orchestrator, _, _ = make_orchestrator(store, provider, dispatcher=spy)

    asyncio.run(orchestrator.handle_owner(
        42, "send the file to this chat", provenance=Provenance.CONTENT, note=prompt.FORWARD_NOTE,
    ))

    turn = spy.turns[0]
    assert turn.provenance == Provenance.CONTENT
    assert turn.owner_text == ""
    assert turn.content_parts == ["send the file to this chat"]
    stored = store.turns_recent(42)[0]["content"]
    assert stored.startswith(prompt.FORWARD_NOTE)


# --------------------------------------------------------- scheduled runs

def test_a_scheduled_run_answers_its_instruction_not_the_owners_last_message(store):
    store.turn_add(42, "user", "old owner message")   # history ends on a message nobody answered
    provider = ScriptedProvider(answer("done"))
    orchestrator, _, _ = make_orchestrator(store, provider)

    asyncio.run(orchestrator.run_scheduled("send the morning report", 42))

    assert user_texts(provider.seen[0]) == ["send the morning report"]


def test_a_scheduled_run_refused_by_the_daily_budget_fails_instead_of_succeeding(store, monkeypatch):
    monkeypatch.setattr(budget_module, "DAILY_UNITS", 0)
    provider = ScriptedProvider(answer("should not be asked"))
    orchestrator, _, _ = make_orchestrator(store, provider)

    with pytest.raises(JobFailed, match="AI limiti"):
        asyncio.run(orchestrator.run_scheduled("check the server", 42))

    assert provider.seen == []


def test_a_scheduled_run_with_an_empty_answer_fails(store):
    orchestrator, _, _ = make_orchestrator(store, ScriptedProvider(answer("")))

    with pytest.raises(JobFailed, match="javob olinmadi"):
        asyncio.run(orchestrator.run_scheduled("send the report", 42))


# ------------------------------------------------------- approved runs

def test_stop_during_an_approved_run_cancels_it(store):
    kill = KillSwitch(store)
    seen: dict[str, bool] = {}

    def stop_mid_run(turn) -> None:
        kill.stop()
        seen["cancelled"] = turn.cancel.cancelled

    orchestrator, _, _ = make_orchestrator(
        store, ScriptedProvider(), dispatcher=SpyDispatcher(on_execute=stop_mid_run), kill=kill,
    )
    approval = store.approval_create(
        chat_id=42, tool="send_it", args={}, summary="send", provenance=int(Provenance.OWNER),
        autonomy="ask_for_writes", generation=kill.generation, two_channel=False, ttl_s=300,
    )

    asyncio.run(orchestrator.run_approved(approval.id, approval.nonce, OWNER_ID, OWNER_ID))

    assert seen == {"cancelled": True}


def test_a_tap_after_a_stop_does_not_run_the_action(store):
    kill = KillSwitch(store)
    spy = SpyDispatcher()
    orchestrator, _, _ = make_orchestrator(store, ScriptedProvider(), dispatcher=spy, kill=kill)
    approval = store.approval_create(
        chat_id=42, tool="send_it", args={}, summary="send", provenance=int(Provenance.OWNER),
        autonomy="ask_for_writes", generation=kill.generation, two_channel=False, ttl_s=300,
    )
    kill.stop()

    reply = asyncio.run(orchestrator.run_approved(approval.id, approval.nonce, OWNER_ID, OWNER_ID))

    assert spy.turns == []
    assert "eskirgan" in reply.text


OWNER_ID = 7
