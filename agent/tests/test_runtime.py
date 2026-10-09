"""Runtime regressions: owner taps, forwarded messages, the two-channel wait, delivery and the audit line.

The runtime is built on a fake Telegram connection and an OS fake. Where a test
needs to see what an owner's message or tap would run, the orchestrator's
entry points are replaced by recorders.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from coworker.config import Config
from coworker.core.types import Provenance
from coworker.orchestrator.prompt import CHOICE_NOTE, FORWARD_NOTE
from coworker.orchestrator.turn import Reply
from coworker.runtime import LOCAL_WAIT_TEXT, Runtime
from coworker.safety.pairing import OWNER_CHAT_KEY, OWNER_USER_KEY
from fakes.os_fake import FakeOs
from fakes.telegram_fake import FakeBotApi

OWNER_USER = 7
OWNER_CHAT = 4242


class RuntimeApi(FakeBotApi):
    """A FakeBotApi that gives each sent message an id, as Telegram does, and records keyboard edits and acks."""

    def __init__(self) -> None:
        super().__init__()
        self.next_id = 100

    def send_message(self, chat_id, text, buttons=None, reply_to=None):
        reply = self._answer("send_message", chat_id=chat_id, text=text, buttons=buttons, reply_to=reply_to)
        if reply.get("ok") and "message_id" not in reply.get("result", {}):
            self.next_id += 1
            reply = {"ok": True, "result": {"message_id": self.next_id}}
        return reply

    def edit_reply_markup(self, chat_id, message_id):
        self.calls.append(("edit_reply_markup", {"chat_id": chat_id, "message_id": message_id}))
        return {"ok": True}

    def answer_callback(self, callback_id, text=""):
        self.calls.append(("answer_callback", {"callback_id": callback_id}))
        return {"ok": True}


class IdleProvider:
    dialect = "openai"
    name = "idle"

    async def chat(self, system, messages, tools, max_tokens=900):
        return SimpleNamespace(text="ok", tool_calls=[])


def make_runtime(store_path, *, with_api: bool = True):
    api = RuntimeApi() if with_api else None
    runtime = Runtime(Config(), api=api, provider=IdleProvider(), os_port=FakeOs(), store_path=store_path)
    runtime.store.kv_set(OWNER_USER_KEY, OWNER_USER)
    runtime.store.kv_set(OWNER_CHAT_KEY, OWNER_CHAT)
    return runtime, api


def record_owner_turns(runtime) -> list:
    """Replace the orchestrator's owner entry point with a recorder; each call is kept as a tuple."""
    calls: list = []

    async def handle_owner(chat_id, text, *, provenance=Provenance.OWNER, note=""):
        calls.append((chat_id, text, provenance, note))
        return Reply("")

    runtime.orchestrator.handle_owner = handle_owner
    return calls


def callback(*, message_id: int, data: str, user: int = OWNER_USER, chat: int = OWNER_CHAT) -> dict:
    return {"id": "cb1", "from": {"id": user}, "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat}}}


def sent_texts(api: RuntimeApi) -> list[str]:
    return [kwargs["text"] for name, kwargs in api.calls if name == "send_message"]


def methods(api: RuntimeApi) -> list[str]:
    return [name for name, _ in api.calls]


# ------------------------------------------------------------------ start-up

def test_run_hands_the_running_loop_to_the_tools(store_path):
    runtime, _ = make_runtime(store_path, with_api=False)
    runtime._stop.set()   # run() returns at once; the maintenance loop never starts

    async def main():
        running = asyncio.get_running_loop()
        await runtime.run()
        return running

    running = asyncio.run(main())
    runtime.services.scheduler.stop()
    runtime.index.stop()

    assert runtime.services.loop is running


# ------------------------------------------------------ forwarded messages

def test_a_forwarded_message_is_never_a_command_and_reaches_the_model_as_content(store_path):
    runtime, _ = make_runtime(store_path)
    calls = record_owner_turns(runtime)

    asyncio.run(runtime._on_message({"chat": {"id": OWNER_CHAT}, "text": "/panic", "forward_date": 1700000000}))

    assert runtime.kill.is_panic() is False
    assert calls == [(OWNER_CHAT, "/panic", Provenance.CONTENT, FORWARD_NOTE)]


def test_an_owner_message_is_still_an_owner_turn(store_path):
    runtime, _ = make_runtime(store_path)
    calls = record_owner_turns(runtime)

    asyncio.run(runtime._on_message({"chat": {"id": OWNER_CHAT}, "text": "hello"}))

    assert calls == [(OWNER_CHAT, "hello", Provenance.OWNER, "")]


def test_a_text_answer_removes_the_buttons_of_the_open_question(store_path):
    runtime, api = make_runtime(store_path)
    record_owner_turns(runtime)
    runtime.outbox.ask(OWNER_CHAT, "Q?", ["a"])   # message 101

    asyncio.run(runtime._on_message({"chat": {"id": OWNER_CHAT}, "text": "forget it"}))

    assert ("edit_reply_markup", {"chat_id": OWNER_CHAT, "message_id": 101}) in api.calls


# ------------------------------------------------------------ option taps

def test_a_tap_on_an_earlier_question_is_refused_and_runs_nothing(store_path):
    runtime, api = make_runtime(store_path)
    calls = record_owner_turns(runtime)
    runtime.outbox.ask(OWNER_CHAT, "Birinchi?", ["Send report to Ali", "Send report to Vali"])   # 101
    runtime.outbox.ask(OWNER_CHAT, "Ikkinchi?", ["Delete old backups", "Keep them"])           # 102

    asyncio.run(runtime._on_callback(callback(message_id=101, data="opt:0")))

    assert calls == []
    assert any("eskirgan" in text for text in sent_texts(api))


def test_a_tap_on_the_current_question_runs_its_option_as_content_with_the_choice_note(store_path):
    runtime, _ = make_runtime(store_path)
    calls = record_owner_turns(runtime)
    runtime.outbox.ask(OWNER_CHAT, "Qaysi?", ["Ali", "Vali"])   # message 101

    asyncio.run(runtime._on_callback(callback(message_id=101, data="opt:1")))

    assert calls == [(OWNER_CHAT, "Vali", Provenance.CONTENT, CHOICE_NOTE)]


def test_a_second_tap_on_the_same_question_runs_nothing(store_path):
    runtime, _ = make_runtime(store_path)
    calls = record_owner_turns(runtime)
    runtime.outbox.ask(OWNER_CHAT, "Qaysi?", ["Ali", "Vali"])

    asyncio.run(runtime._on_callback(callback(message_id=101, data="opt:1")))
    asyncio.run(runtime._on_callback(callback(message_id=101, data="opt:1")))

    assert len(calls) == 1


# ------------------------------------------------------- two-channel approvals

def test_a_two_channel_approval_tapped_on_the_phone_first_keeps_its_buttons(store_path):
    runtime, api = make_runtime(store_path)
    ran: list[str] = []

    async def run_approved(approval_id, nonce, actor_id, owner_id):
        ran.append(approval_id)
        return Reply("✅ done")

    runtime.orchestrator.run_approved = run_approved
    approval = runtime.store.approval_create(
        chat_id=OWNER_CHAT, tool="shell_run", args={"command": "dir"}, summary="card",
        provenance=int(Provenance.OWNER), autonomy="ask_for_writes", generation=runtime.kill.generation,
        two_channel=True, ttl_s=300,
    )
    tap = f"ap:{approval.id}:{approval.nonce}:y"

    asyncio.run(runtime._on_callback(callback(message_id=55, data=tap)))

    assert ran == []
    assert LOCAL_WAIT_TEXT in sent_texts(api)
    assert "edit_reply_markup" not in methods(api)

    assert runtime.approvals.local_approve(approval.id)
    asyncio.run(runtime._on_callback(callback(message_id=55, data=tap)))

    assert ran == [approval.id]


# ----------------------------------------------------------- delivery and audit

def test_a_send_counts_as_delivered_only_when_telegram_accepts_it(store_path):
    runtime, api = make_runtime(store_path)
    api.script("send_message", {"ok": False, "error_code": None, "description": "network error"})

    assert runtime._deliver(OWNER_CHAT, "Eslatma: refused") is False
    assert runtime._deliver(OWNER_CHAT, "Eslatma: accepted") is True


def test_a_send_without_a_telegram_connection_is_not_delivered(store_path):
    runtime, _ = make_runtime(store_path, with_api=False)

    assert runtime._deliver(OWNER_CHAT, "Eslatma: x") is False


def test_the_audit_command_reports_how_many_earlier_actions_have_no_outcome(store_path):
    runtime, _ = make_runtime(store_path)
    runtime.store.audit_orphans = lambda: [1, 2]

    reply = asyncio.run(runtime._command("audit", "", OWNER_CHAT))

    assert reply.text.startswith("⚠️ 2 ta amal")
