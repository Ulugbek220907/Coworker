"""Writing into an app: typing without sending, sending only what is on screen, honest messages.

The window and the keyboard are replaced by fakes, and the on-screen check is replaced
by a fixed answer, so nothing here touches a real window.
"""
from __future__ import annotations

import pytest

from coworker.core.types import Autonomy, CallContext, Provenance
from coworker.orchestrator.turn import result_text
from coworker.tools import desktop
from coworker.tools.registry import Services, ToolCall, build_registry
from coworker.core.types import ToolResult


@pytest.fixture
def window(monkeypatch):
    calls: dict[str, list] = {"typed": [], "enter": []}
    monkeypatch.setattr(desktop.uia, "find_window", lambda app: {"handle": 7, "title": "Antigravity"})
    monkeypatch.setattr(desktop.keys, "type_text", lambda text, handle, blocked=(): calls["typed"].append(text) or {"ok": True})
    monkeypatch.setattr(desktop.keys, "press", lambda combo, handle, blocked=(): calls["enter"].append(combo) or {"ok": True})
    return calls


def _ctx() -> CallContext:
    return CallContext(
        turn_id="t", actor="owner", chat_id=1, autonomy=Autonomy.ASK_FOR_WRITES,
        grants=frozenset({"desktop_control"}), generation=0, provenance=Provenance.OWNER,
    )


def _run(handler, args: dict) -> ToolResult:
    return handler(ToolCall(name="x", args=args, ctx=_ctx(), svc=Services()))


def test_typing_into_an_app_does_not_press_enter(window, monkeypatch):
    monkeypatch.setattr(desktop, "_text_on_screen", lambda *a, **k: True)
    result = _run(desktop._control_app_type, {"app": "Antigravity", "text": "finish this project"})
    assert result.ok is True
    assert window["typed"] == ["finish this project"]
    assert window["enter"] == [], "typing must never press Enter"
    assert "Enter bosilmadi" in result.data["message"]


def test_typing_that_is_not_on_screen_is_reported_as_not_verified(window, monkeypatch):
    monkeypatch.setattr(desktop, "_text_on_screen", lambda *a, **k: False)
    result = _run(desktop._control_app_type, {"app": "Antigravity", "text": "finish this project"})
    assert result.ok is False
    assert result.code == "not_verified"
    assert "ko'rinmadi" in result.error


def test_send_refuses_to_press_enter_when_the_text_is_not_on_screen(window, monkeypatch):
    monkeypatch.setattr(desktop, "_text_on_screen", lambda *a, **k: False)
    result = _run(desktop._control_app_send, {"app": "Antigravity", "text": "hello"})
    assert result.ok is False
    assert window["enter"] == [], "nothing is sent blind"


def test_send_presses_enter_once_the_text_is_seen(window, monkeypatch):
    monkeypatch.setattr(desktop, "_text_on_screen", lambda *a, **k: True)
    result = _run(desktop._control_app_send, {"app": "Antigravity", "text": "hello"})
    assert result.ok is True
    assert window["enter"] == ["enter"]
    assert result.data["verified"] is True


def test_when_the_check_cannot_be_made_the_message_says_so(window, monkeypatch):
    monkeypatch.setattr(desktop, "_text_on_screen", lambda *a, **k: None)
    result = _run(desktop._control_app_type, {"app": "Antigravity", "text": "hi"})
    assert result.ok is True
    assert "tekshirib bo'lmadi" in result.data["message"]


def test_clipboard_says_it_wrote_nothing_into_an_app(monkeypatch):
    monkeypatch.setattr(desktop.keys, "clipboard_set", lambda text: {"ok": True, "chars": len(text)})
    result = _run(desktop._clipboard_set, {"text": "finish this project"})
    assert result.ok is True
    assert result.data["message"] == "Bufer ustiga yozildi (19 belgi). Hech qaysi ilovaga yozilmadi."


def test_the_model_is_told_which_tool_types_into_an_app():
    registry = build_registry()
    clipboard = registry.get("clipboard_set")
    typing = registry.get("control_app_type")
    assert "control_app_type" in clipboard.description
    assert typing is not None and typing.tier.value == "LOCAL_WRITE" and typing.family == "desktop_control"


def test_the_owner_reads_what_the_tool_verified_not_a_bare_done():
    with_message = ToolResult(ok=True, data={"message": "Matn «Antigravity» oynasiga yozildi."})
    assert result_text(with_message, "Matn yoziladi") == "✅ Matn «Antigravity» oynasiga yozildi."
    without = ToolResult(ok=True, data={})
    assert result_text(without, "«Antigravity» ilovasiga yoziladi") == "✅ Bajarildi: «Antigravity» ilovasiga yoziladi"
    assert result_text(without, "") == "✅ Bajarildi."
