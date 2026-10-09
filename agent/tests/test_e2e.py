"""End to end through the real Runtime, with fakes only at the edges.

The Telegram client, the model and the operating system are fakes. Everything in
between is real: pairing, the owner gate, message routing, the orchestrator loop,
the dispatcher, the policy kernel, approvals, the store and the outbox. These are
the seams where the unit tests cannot see a wiring mistake.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coworker import launcher
from coworker.config import Config
from coworker.core.ports import PowerState
from coworker.llm_base import STOP_END, STOP_TOOL, ToolCallReq, Turn
from coworker.runtime import Runtime

OWNER = 42
STRANGER = 99
OWNER_CHAT = 42


class FakeApi:
    """Records what the bot would have sent. Only the methods the runtime uses."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.answered: list[str] = []
        self.edits: list[int] = []

    def send_message(self, chat_id, text, buttons=None, reply_to=None) -> dict:
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons})
        return {"ok": True, "result": {"message_id": len(self.sent)}}

    def send_document(self, chat_id, path, caption="") -> dict:
        return {"ok": True}

    def send_photo(self, chat_id, path, caption="") -> dict:
        return {"ok": True}

    def answer_callback(self, callback_id: str, text: str = "") -> dict:
        self.answered.append(callback_id)
        return {"ok": True}

    def edit_reply_markup(self, chat_id: int, message_id: int) -> dict:
        self.edits.append(message_id)
        return {"ok": True}

    def get_file_bytes(self, file_id: str, max_bytes: int = 0) -> dict:
        return {"ok": False}

    def delete_webhook(self) -> dict:
        return {"ok": True}

    def close(self) -> None:
        pass


class ScriptedProvider:
    """Returns the scripted model turns in order and records each request."""

    name = "scripted"
    dialect = "openai"

    def __init__(self, script: list[Turn]) -> None:
        self.script = list(script)
        self.requests: list[list] = []

    async def chat(self, system, messages, tools, *, max_tokens, temperature=0.2) -> Turn:
        self.requests.append(list(messages))
        if not self.script:
            return Turn(text="(no more scripted turns)", tool_calls=[], stop_reason=STOP_END, usage={})
        return self.script.pop(0)

    async def vision(self, prompt, image_b64, *, max_tokens=1200) -> str:
        return ""

    async def close(self) -> None:
        pass


class CalmOs:
    """A machine with plenty of room, so the governor never pauses a test."""

    def idle_seconds(self) -> float:
        return 600.0

    def cpu_percent(self) -> float:
        return 5.0

    def free_ram_mb(self) -> float:
        return 8192.0

    def power(self) -> PowerState:
        return PowerState(percent=None, plugged=None, saver=False)

    def is_elevated(self) -> bool:
        return False

    def input_desktop_available(self) -> bool:
        return True


def _tool(name: str, args: dict, call_id: str = "t1") -> Turn:
    return Turn(text="", tool_calls=[ToolCallReq(id=call_id, name=name, args=args)],
                stop_reason=STOP_TOOL, usage={})


def _say(text: str) -> Turn:
    return Turn(text=text, tool_calls=[], stop_reason=STOP_END, usage={})


def _message(text: str, *, chat_id: int = OWNER_CHAT, from_id: int = OWNER, message_id: int = 1) -> dict:
    return {"message_id": message_id, "chat": {"id": chat_id, "type": "private"},
            "from": {"id": from_id}, "text": text}


def _callback(data: str, *, from_id: int = OWNER, message_id: int = 5) -> dict:
    return {"id": "cb-" + data[-6:], "from": {"id": from_id},
            "message": {"message_id": message_id, "chat": {"id": OWNER_CHAT, "type": "private"}},
            "data": data}


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COWORKER_HOME", str(tmp_path / "home"))
    cfg = Config(tmp_path / "config.json")
    holder: dict = {}

    def build(script: list[Turn]) -> Runtime:
        provider = ScriptedProvider(script)
        rt = Runtime(cfg, api=FakeApi(), provider=provider, os_port=CalmOs(),
                     store_path=tmp_path / "coworker.db")
        rt._loop = asyncio.new_event_loop()
        holder["rt"] = rt
        holder["provider"] = provider
        return rt

    yield build
    rt = holder.get("rt")
    if rt is not None:
        rt.kill.stop()
        rt.services.scheduler.stop()
        rt._loop.close()


def _pair(rt: Runtime) -> None:
    code = rt.pairing.issue_code()
    ok, _ = rt.pairing.redeem(code, from_id=OWNER, chat_id=OWNER_CHAT, chat_type="private")
    assert ok, "pairing with the issued code must succeed"


def test_pairing_pins_the_owner_and_the_gate_drops_strangers(runtime):
    rt = runtime([])
    _pair(rt)
    assert rt.pairing.owner() == (OWNER, OWNER_CHAT)
    assert rt.gate.allows({"message": _message("salom")}) is True
    assert rt.gate.allows({"message": _message("salom", from_id=STRANGER, chat_id=STRANGER)}) is False


def test_an_owner_message_gets_the_models_answer_back_in_the_chat(runtime):
    rt = runtime([_say("Salom, men tayyorman.")])
    _pair(rt)
    asyncio.run(rt._on_message(_message("salom")))
    assert rt.outbox is not None
    replies = [m for m in rt.api.sent if m["chat_id"] == OWNER_CHAT]
    assert replies and replies[-1]["text"] == "Salom, men tayyorman."


def test_a_tool_call_runs_and_its_result_reaches_the_model(runtime):
    rt = runtime([_tool("note_add", {"title": "Reja", "body": "Dushanba: hisobot"}), _say("Saqlandi.")])
    _pair(rt)
    asyncio.run(rt._on_message(_message("buni yozib qo'y")))
    notes = rt.store.notes_list(OWNER_CHAT)
    assert [n["title"] for n in notes] == ["Reja"]
    provider = rt.orchestrator.svc.llm
    second_request = provider.requests[1]
    assert any(getattr(m, "role", "") == "tool" and "ok" in (m.content or "") for m in second_request)
    assert rt.api.sent[-1]["text"] == "Saqlandi."


def test_a_risky_action_becomes_a_card_and_only_the_tap_runs_it_once(runtime, monkeypatch):
    launched: list[str] = []

    def fake_launch(query: str) -> dict:
        launched.append(query)
        return {"ok": True, "started": query}

    monkeypatch.setattr(launcher, "launch", fake_launch)
    rt = runtime([_tool("open_app", {"name": "zzqqnoapp"}), _say("Ochildi.")])
    _pair(rt)

    asyncio.run(rt._on_message(_message("zzqqnoapp ni och")))
    card = rt.api.sent[-1]
    assert card["text"].startswith("⚠️ Tasdiq kerak"), card["text"]
    assert launched == [], "nothing runs before the owner taps"
    approve = next(b["callback_data"] for row in card["buttons"] for b in row
                   if b["callback_data"].endswith(":y"))

    asyncio.run(rt._on_callback(_callback(approve)))
    assert launched == ["zzqqnoapp"]

    asyncio.run(rt._on_callback(_callback(approve, message_id=6)))
    assert launched == ["zzqqnoapp"], "a replayed tap must not run the action again"
    assert "eskirgan" in rt.api.sent[-1]["text"]


def test_panic_stops_a_tool_call_before_it_runs(runtime):
    rt = runtime([_tool("note_add", {"title": "Yashirin", "body": "x"}), _say("Bajarilmadi.")])
    _pair(rt)
    asyncio.run(rt._on_message(_message("/panic")))
    assert rt.kill.is_panic() is True

    asyncio.run(rt._on_message(_message("eslab qol")))
    assert rt.store.notes_list(OWNER_CHAT) == [], "no note may be written while panic is on"
    provider = rt.orchestrator.svc.llm
    second_request = provider.requests[1]
    assert any(getattr(m, "role", "") == "tool" and "panic" in (m.content or "") for m in second_request)


def test_a_stranger_cannot_reach_a_command_or_a_card(runtime):
    rt = runtime([])
    _pair(rt)
    stranger = {"message": _message("/panic", from_id=STRANGER, chat_id=STRANGER)}
    assert rt.gate.allows(stranger) is False
    assert rt.kill.is_panic() is False
