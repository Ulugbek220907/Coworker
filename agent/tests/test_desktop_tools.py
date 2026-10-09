"""Desktop tools: the spec table, schema limits, label-to-tier rules, relax and escalation, handler wiring.

The flat modules (uia, keys, vision) are replaced attribute by attribute, so
these tests see the tool layer's decisions and nothing that touches Windows.

policy/prohibited.py belongs to another task. If it is not written yet, a small
stand-in with the contract's signatures is installed here, so the tool layer can
still be imported and tested; the real module is used as soon as it exists.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import types
from dataclasses import replace

import pytest

try:
    from coworker.policy import prohibited  # noqa: F401
except ImportError:
    _fake_prohibited = types.ModuleType("coworker.policy.prohibited")
    _fake_prohibited.scan_args = lambda args: None
    _fake_prohibited.redact = lambda text: text
    sys.modules["coworker.policy.prohibited"] = _fake_prohibited
    import coworker.policy as _policy_package
    _policy_package.prohibited = _fake_prohibited

from coworker import keys, uia, vision  # noqa: E402
from coworker.core.types import Autonomy, CallContext, Decision, Provenance, Tier, normalize_text  # noqa: E402
from coworker.policy.kernel import PolicyKernel  # noqa: E402
from coworker.tools import desktop  # noqa: E402
from coworker.tools.registry import Registry, Services, ToolCall, validate_args  # noqa: E402


# -------------------------------------------------------------- helpers

def ctx() -> CallContext:
    return CallContext(
        turn_id="t1", actor="owner", chat_id=1, autonomy=Autonomy.ASK_FOR_WRITES,
        grants=frozenset({"desktop", "desktop_control"}), generation=0, provenance=Provenance.OWNER,
    )


class Config:
    def __init__(self, blocked=()) -> None:
        self._blocked = list(blocked)

    def get(self, key, default=None):
        return self._blocked if key == "blocked_windows" else default


class FakeLlm:
    def __init__(self, answer: str = "ok", error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def vision(self, prompt: str, image_b64: str, *, max_tokens: int = 1200) -> str:
        self.calls.append((prompt, image_b64))
        if self.error is not None:
            raise self.error
        return self.answer


def services(llm=None, blocked=(), loop=None) -> Services:
    return Services(config=Config(blocked), llm=llm, loop=loop)


@pytest.fixture
def runtime_loop():
    """An asyncio loop on its own thread, standing in for the runtime loop the model client lives on."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


def spec(name: str):
    return next(s for s in desktop.SPECS if s.name == name)


def run(name: str, args: dict, svc: Services | None = None):
    call = ToolCall(name=name, args=args, ctx=ctx(), svc=svc or services())
    return spec(name).handler(call)


def element(ref: int, kind: str, name: str, *, password: bool = False) -> uia.Element:
    return uia.Element(ref=ref, type=kind, name=name, path=(ref - 1,), is_password=password)


@pytest.fixture
def snapshot_of(monkeypatch):
    """Install a cached snapshot for window 7, as read_window would have left it."""
    def install(*elements: uia.Element, handle: int = 7) -> None:
        monkeypatch.setattr(uia, "_snapshots", {handle: uia.Snapshot(handle=handle, elements=list(elements))})
    return install


def _must_not_run(*args, **kwargs):
    raise AssertionError("this layer must not be reached")


# ------------------------------------------------------- the contract rows

EXPECTED_ROWS = {
    "list_windows": ("desktop", Tier.READ, "UIA"),
    "read_window": ("desktop", Tier.READ, "UIA"),
    "screen_read": ("desktop", Tier.READ, "VISION"),
    "clipboard_get": ("desktop", Tier.READ, "INPUT"),
    "control_app_read": ("desktop", Tier.READ, "VISION"),
    "ui_set_text": ("desktop_control", Tier.LOCAL_WRITE, "UIA"),
    "key_type": ("desktop_control", Tier.LOCAL_WRITE, "INPUT"),
    "key_press": ("desktop_control", Tier.LOCAL_WRITE, "INPUT"),
    "ui_click": ("desktop_control", Tier.LOCAL_WRITE, "UIA"),
    "clipboard_set": ("desktop_control", Tier.LOCAL_WRITE, "INPUT"),
    "control_app_send": ("desktop_control", Tier.OUTBOUND, "INPUT"),
}


def test_specs_match_the_contract_rows() -> None:
    got = {s.name: (s.family, s.tier, s.gov_class) for s in desktop.SPECS}
    assert got == EXPECTED_ROWS


def test_only_the_reads_that_return_outside_text_are_untrusted() -> None:
    untrusted = {s.name for s in desktop.SPECS if s.untrusted}
    assert untrusted == {"read_window", "screen_read", "clipboard_get", "control_app_read"}


def test_no_spec_is_financial_or_credential() -> None:
    assert not any(s.tier in (Tier.FINANCIAL, Tier.CREDENTIAL) for s in desktop.SPECS)


def test_the_registry_accepts_every_spec() -> None:
    registry = Registry()
    registry.register_many(desktop.SPECS)
    assert len(registry.all()) == len(EXPECTED_ROWS)


def test_control_app_send_is_the_only_outbound_row_and_names_its_sensitive_args() -> None:
    send = spec("control_app_send")
    assert send.tier == Tier.OUTBOUND
    assert set(send.sensitive_args) == {"app", "text"}


def test_relax_hooks_exist_only_for_key_press_and_ui_click() -> None:
    with_relax = {s.name for s in desktop.SPECS if s.relax is not None}
    assert with_relax == {"key_press", "ui_click"}


# ------------------------------------------------------------ schema limits

def test_a_handle_of_zero_is_refused_by_the_schema() -> None:
    error = validate_args(spec("key_press").parameters, {"handle": 0, "combo": "ctrl+s"})
    assert error is not None and "at least 1" in error


def test_typed_text_over_the_cap_is_refused_by_the_schema() -> None:
    error = validate_args(spec("key_type").parameters, {"handle": 7, "text": "a" * 2001})
    assert error is not None and "too long" in error


def test_unknown_arguments_are_refused_by_the_schema() -> None:
    assert validate_args(spec("list_windows").parameters, {"all": True}) is not None


# ------------------------------------------------------ label to tier rules

LABELS = [
    (1, "Button", "Save", "plain"),
    (2, "Button", "Delete", "CONFIRM:label_destructive"),
    (3, "Button", "Pay now", "DENY:prohibited_tier"),
    (4, "Button", "Sign in", "CONFIRM:label_credential"),
    (5, "Button", "Send", "CONFIRM:label_outbound"),
    (6, "Button", "Restart", "CONFIRM:label_system_change"),
    (7, "Button", "", "CONFIRM:unlabelled_control"),
]


@pytest.mark.parametrize("ref,kind,name,expected", LABELS)
def test_ui_click_label_maps_to_its_tier(snapshot_of, ref, kind, name, expected) -> None:
    snapshot_of(element(ref, kind, name))
    args = {"handle": 7, "ref": ref, "name": name}
    verdict = desktop._check_ui_click(args, ctx(), None)
    if expected == "plain":
        assert verdict is None
        assert desktop._relax_ui_click(args, ctx()) is True
        return
    assert desktop._relax_ui_click(args, ctx()) is False
    decision, code = expected.split(":")
    assert verdict is not None
    assert verdict.decision == Decision(decision)
    assert verdict.code == code


def test_a_password_field_is_never_relaxed_even_with_a_plain_label(snapshot_of) -> None:
    snapshot_of(element(8, "Edit", "Code", password=True))
    assert desktop._relax_ui_click({"handle": 7, "ref": 8}, ctx()) is False


def test_an_unknown_element_is_not_relaxed(snapshot_of) -> None:
    snapshot_of(element(1, "Button", "Save"))
    assert desktop._relax_ui_click({"handle": 7, "ref": 99}, ctx()) is False
    assert desktop._check_ui_click({"handle": 7, "ref": 99}, ctx(), None) is None


def test_ui_set_text_refuses_a_password_field(snapshot_of) -> None:
    snapshot_of(element(2, "Edit", "Password", password=True))
    verdict = desktop._check_ui_set_text({"handle": 7, "ref": 2, "text": "x"}, ctx(), None)
    assert verdict.decision == Decision.DENY
    assert verdict.code == "password_field"


def test_ui_set_text_allows_an_ordinary_field(snapshot_of) -> None:
    snapshot_of(element(2, "Edit", "Name"))
    assert desktop._check_ui_set_text({"handle": 7, "ref": 2, "text": "x"}, ctx(), None) is None


# ------------------------------------------------------ key press rules

@pytest.mark.parametrize("combo,relaxed", [
    ("ctrl+s", True),
    ("ctrl+c", True),
    ("f5", True),
    ("alt+tab", True),
    ("enter", False),
    ("Return", False),
    ("ctrl+shift+enter", False),
    ("esc", False),
    ("escape", False),
    ("alt+f4", False),
    ("ctrl+w", False),
    ("ctrl+f4", False),
    ("shift+delete", False),
    ("garbage", False),
])
def test_key_press_relaxes_only_benign_combinations(combo: str, relaxed: bool) -> None:
    assert desktop._relax_key_press({"handle": 7, "combo": combo}, ctx()) is relaxed


def test_enter_escalates_to_two_channel_confirmation() -> None:
    verdict = desktop._check_key_press({"handle": 7, "combo": "Enter"}, ctx(), None)
    assert verdict.decision == Decision.CONFIRM
    assert verdict.two_channel is True
    assert verdict.summary


def test_ordinary_key_press_gets_no_extra_verdict() -> None:
    assert desktop._check_key_press({"handle": 7, "combo": "ctrl+s"}, ctx(), None) is None


# --------------------------------------------- terminal and IDE targets

@pytest.mark.parametrize("title,image,two_channel", [
    ("Windows PowerShell", "powershell.exe", True),
    ("Visual Studio Code", "code.exe", True),
    ("Antigravity", "antigravity.exe", True),
    ("Command Prompt", "cmd.exe", True),
    ("IntelliJ IDEA", "idea64.exe", True),
    ("MINGW64:/c/Users/user", "mintty.exe", True),
    ("user@DESKTOP-ABC: ~", "ubuntu2204.exe", True),
    ("Work notes", "windowsterminal.exe", True),
    ("Telegram", "telegram.exe", False),
    ("Notes", "notepad.exe", False),
])
def test_control_app_send_is_two_channel_for_terminals_and_editors(monkeypatch, title, image, two_channel) -> None:
    monkeypatch.setattr(uia, "find_window", lambda name: {"handle": 9, "title": title, "pid": 11})
    monkeypatch.setattr(uia, "process_image", lambda pid: image)
    verdict = desktop._check_app_send({"app": title, "text": "hi"}, ctx(), None)
    assert verdict.decision == Decision.CONFIRM
    assert verdict.two_channel is two_channel


def test_a_terminal_without_a_readable_program_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(uia, "find_window", lambda name: {"handle": 9, "title": "Telegram", "pid": 11})
    monkeypatch.setattr(uia, "process_image", lambda pid: "")
    verdict = desktop._check_app_send({"app": "Telegram", "text": "hi"}, ctx(), None)
    assert verdict.two_channel is True
    assert verdict.summary


def test_the_title_alone_still_marks_a_terminal_without_a_program_name() -> None:
    assert desktop.looks_like_terminal_or_ide("MINGW64:/c/Users/user") is True
    assert desktop.looks_like_terminal_or_ide("user@DESKTOP-ABC: ~ (Ubuntu)") is True
    assert desktop.looks_like_terminal_or_ide("Notes") is False


def test_an_unidentified_target_fails_closed_to_two_channel(monkeypatch) -> None:
    monkeypatch.setattr(uia, "find_window", lambda name: {"error": "ikki oyna", "code": "ambiguous_window"})
    verdict = desktop._check_app_send({"app": "Notes", "text": "hi"}, ctx(), None)
    assert verdict.two_channel is True
    assert verdict.decision == Decision.CONFIRM


# -------------------------------------------------------------- summaries

def test_every_summary_hook_returns_owner_text() -> None:
    sample = {"handle": 7, "ref": 1, "text": "matn", "combo": "ctrl+s", "app": "Notes"}
    for s in desktop.SPECS:
        if s.summary is not None:
            assert isinstance(s.summary(sample), str) and s.summary(sample)


def test_key_press_summary_names_the_normalised_combo() -> None:
    assert desktop._summary_key_press({"combo": "Ctrl+S"}) == "Tugmalar bosiladi: ctrl+s"
    assert "Enter" in desktop._summary_key_press({"combo": "enter"})
    assert "Noma'lum" in desktop._summary_key_press({"combo": "ctrl+%"})


def test_key_type_summary_shows_the_whole_text_and_names_the_window() -> None:
    text = "a" * desktop.CARD_TEXT
    summary = desktop._summary_key_type({"text": text}, "Notes")
    assert text in summary
    assert "«Notes»" in summary


# ------------------------------------------------------ handler: observing

def test_list_windows_wraps_the_layer_result(monkeypatch) -> None:
    monkeypatch.setattr(uia, "list_windows", lambda: {"windows": [{"title": "Notes", "handle": 7}], "count": 1})
    result = run("list_windows", {})
    assert result.ok is True
    assert result.data["windows"] == [{"title": "Notes", "handle": 7}]
    # Window titles come from the apps and pages that own them: content, not owner words.
    assert result.untrusted is True


def test_read_window_needs_a_handle_or_a_title() -> None:
    result = run("read_window", {})
    assert result.ok is False
    assert result.code == "arg_invalid"


def test_read_window_passes_the_owner_blocked_list_and_is_untrusted(monkeypatch) -> None:
    captured = {}

    def read(title="", handle=0, limit=0, blocked=()):
        captured.update(title=title, handle=handle, blocked=tuple(blocked))
        return {"window": "Notes", "handle": 3, "elements": '[1] Button "OK"', "count": 1}

    monkeypatch.setattr(uia, "read_window", read)
    result = run("read_window", {"handle": 3}, services(blocked=["Acme Vault"]))
    assert result.ok and result.untrusted
    assert captured["handle"] == 3
    assert "Acme Vault" in captured["blocked"]
    assert "keepass" in captured["blocked"], "the built-in list must always apply"


def test_read_window_surfaces_an_ambiguous_title_with_candidates(monkeypatch) -> None:
    monkeypatch.setattr(uia, "read_window", lambda **kwargs: {
        "error": "ikki oyna", "code": "ambiguous_window", "candidates": [{"title": "Notes", "handle": 10}],
    })
    result = run("read_window", {"title": "Notes"})
    assert result.ok is False
    assert result.code == "ambiguous_window"
    assert result.to_dict()["candidates"] == [{"title": "Notes", "handle": 10}]


def test_clipboard_get_is_redacted_before_it_is_returned(monkeypatch) -> None:
    monkeypatch.setattr(keys, "clipboard_get", lambda: {"text": "key sk-ABC123", "length": 14})
    seen: list[str] = []
    monkeypatch.setattr(desktop.prohibited, "redact", lambda text: seen.append(text) or "key [redacted]")
    result = run("clipboard_get", {})
    assert seen == ["key sk-ABC123"]
    assert result.data["text"] == "key [redacted]"
    assert result.untrusted is True


def test_clipboard_get_failure_is_a_failed_result(monkeypatch) -> None:
    monkeypatch.setattr(keys, "clipboard_get", lambda: {"error": "Buferni o'qib bo'lmadi."})
    result = run("clipboard_get", {})
    assert result.ok is False
    assert result.error == "Buferni o'qib bo'lmadi."
    assert result.untrusted is False


def test_screen_read_needs_a_vision_model(monkeypatch) -> None:
    monkeypatch.setattr(vision, "capture", _must_not_run)
    result = run("screen_read", {"question": "nima bor?"}, services(llm=None))
    assert result.ok is False
    assert result.code == "not_configured"


def test_screen_read_asks_the_model_about_one_capture(monkeypatch, runtime_loop) -> None:
    seen: dict = {}

    def capture(handle=0, *, blocked=()):
        seen.update(handle=handle, blocked=tuple(blocked))
        return {"image_b64": "QUJD", "size": (10, 10), "scope": "butun ekran"}

    monkeypatch.setattr(vision, "capture", capture)
    llm = FakeLlm(answer="Xatolik xabari bor")
    result = run("screen_read", {"question": "Xato bormi?"}, services(llm=llm, blocked=["Acme"], loop=runtime_loop))
    assert result.ok and result.untrusted
    assert result.data["answer"] == "Xatolik xabari bor"
    assert result.data["scope"] == "butun ekran"
    prompt, image = llm.calls[0]
    assert "SAVOL: Xato bormi?" in prompt
    assert image == "QUJD"
    assert seen["handle"] == 0
    assert "Acme" in seen["blocked"]


def test_screen_read_resolves_a_title_to_its_handle(monkeypatch, runtime_loop) -> None:
    seen: dict = {}
    monkeypatch.setattr(uia, "find_window", lambda title: {"handle": 42, "title": title})
    monkeypatch.setattr(vision, "capture", lambda handle=0, *, blocked=(): seen.update(handle=handle)
                        or {"image_b64": "QQ==", "size": (1, 1), "scope": "Notes"})
    run("screen_read", {"question": "x", "title": "Notes"}, services(llm=FakeLlm(), loop=runtime_loop))
    assert seen["handle"] == 42


def test_screen_read_stops_on_an_ambiguous_title(monkeypatch) -> None:
    monkeypatch.setattr(uia, "find_window", lambda title: {"error": "ikki", "code": "ambiguous_window"})
    monkeypatch.setattr(vision, "capture", _must_not_run)
    result = run("screen_read", {"question": "x", "title": "Notes"}, services(llm=FakeLlm()))
    assert result.code == "ambiguous_window"


def test_screen_read_reports_a_model_failure_without_crashing(monkeypatch, runtime_loop) -> None:
    monkeypatch.setattr(vision, "capture", lambda handle=0, *, blocked=(): {
        "image_b64": "QQ==", "size": (1, 1), "scope": "butun ekran"})
    result = run("screen_read", {"question": "x"}, services(llm=FakeLlm(error=RuntimeError("boom")), loop=runtime_loop))
    assert result.ok is False
    assert "Vision model xatosi" in result.error
    assert result.untrusted is False


def test_control_app_read_uses_the_app_prompt_on_the_app_window(monkeypatch, runtime_loop) -> None:
    monkeypatch.setattr(uia, "find_window", lambda title: {"handle": 9, "title": "Claude Code"})
    seen: dict = {}
    monkeypatch.setattr(vision, "capture", lambda handle=0, *, blocked=(): seen.update(handle=handle)
                        or {"image_b64": "QQ==", "size": (1, 1), "scope": "Claude Code"})
    llm = FakeLlm(answer="oxirgi javob")
    result = run("control_app_read", {"app": "Claude Code"}, services(llm=llm, loop=runtime_loop))
    assert seen["handle"] == 9
    assert llm.calls[0][0] == vision.APP_PROMPT
    assert result.data["app"] == "Claude Code"
    assert result.data["answer"] == "oxirgi javob"
    assert result.untrusted is True


# ----------------------------------------------- handler: acting (wiring)

def test_ui_click_passes_handle_ref_name_and_the_blocked_list(monkeypatch) -> None:
    captured: dict = {}

    def click(handle, ref, blocked=(), expect_name=None):
        captured.update(handle=handle, ref=ref, blocked=tuple(blocked), expect_name=expect_name)
        return {"ok": True, "clicked": "Save", "via": "UIA pattern"}

    monkeypatch.setattr(uia, "click", click)
    result = run("ui_click", {"handle": 7, "ref": 1, "name": "Save"}, services(blocked=["Acme"]))
    assert result.ok is True
    assert captured["handle"] == 7 and captured["ref"] == 1
    assert captured["expect_name"] == "Save"
    assert "Acme" in captured["blocked"]


def test_ui_set_text_keeps_the_text_exactly_as_sent(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(uia, "set_text", lambda handle, ref, text, blocked=(): captured.update(text=text) or {"ok": True})
    run("ui_set_text", {"handle": 7, "ref": 2, "text": "  two  spaces  "})
    assert captured["text"] == "  two  spaces  "


def test_key_type_keeps_the_text_and_passes_the_handle(monkeypatch) -> None:
    captured: dict = {}

    def type_text(text, handle, blocked=()):
        captured.update(text=text, handle=handle)
        return {"ok": True, "typed": len(text), "window": "Notes"}

    monkeypatch.setattr(keys, "type_text", type_text)
    result = run("key_type", {"handle": 7, "text": "salom dunyo "})
    assert result.ok
    assert captured == {"text": "salom dunyo ", "handle": 7}


def test_key_press_passes_the_combo_and_handle(monkeypatch) -> None:
    captured: dict = {}

    def press(combo, handle, blocked=()):
        captured.update(combo=combo, handle=handle)
        return {"ok": True, "pressed": "ctrl+s", "window": "Notes"}

    monkeypatch.setattr(keys, "press", press)
    run("key_press", {"handle": 7, "combo": " Ctrl+S "})
    assert captured == {"combo": "Ctrl+S", "handle": 7}


@pytest.mark.parametrize("name,args", [
    ("ui_click", {"handle": 0, "ref": 1}),
    ("ui_click", {"handle": 7, "ref": 0}),
    ("ui_set_text", {"handle": 0, "ref": 1, "text": "x"}),
    ("key_type", {"handle": 0, "text": "x"}),
    ("key_press", {"handle": 0, "combo": "ctrl+s"}),
])
def test_the_tool_layer_refuses_a_missing_target(monkeypatch, name, args) -> None:
    for module, attr in ((uia, "click"), (uia, "set_text"), (keys, "type_text"), (keys, "press")):
        monkeypatch.setattr(module, attr, _must_not_run)
    result = run(name, args)
    assert result.ok is False
    assert result.code == "arg_invalid"


def test_clipboard_set_routes_the_text_to_the_keys_layer(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(keys, "clipboard_set", lambda text: captured.update(text=text) or {"ok": True, "length": 3})
    result = run("clipboard_set", {"text": "abc"})
    assert result.ok and captured["text"] == "abc"


def test_control_app_send_types_then_presses_enter(monkeypatch) -> None:
    monkeypatch.setattr(uia, "find_window", lambda title: {"handle": 9, "title": "Claude Code"})
    typed: list = []
    pressed: list = []
    monkeypatch.setattr(keys, "type_text", lambda text, handle, blocked=(): typed.append((text, handle))
                        or {"ok": True, "typed": len(text)})
    monkeypatch.setattr(keys, "press", lambda combo, handle, blocked=(): pressed.append((combo, handle))
                        or {"ok": True, "pressed": "enter"})
    result = run("control_app_send", {"app": "Claude Code", "text": "salom"})
    assert result.ok is True
    assert result.data == {"app": "Claude Code", "sent": True}
    assert typed == [("salom", 9)]
    assert pressed == [("enter", 9)]


def test_control_app_send_reports_text_typed_but_enter_not_pressed(monkeypatch) -> None:
    monkeypatch.setattr(uia, "find_window", lambda title: {"handle": 9, "title": "Claude Code"})
    monkeypatch.setattr(keys, "type_text", lambda text, handle, blocked=(): {"ok": True, "typed": len(text)})
    monkeypatch.setattr(keys, "press", lambda combo, handle, blocked=(): {
        "error": "Oynani oldinga chiqarib bo'lmadi.", "sent": False})
    result = run("control_app_send", {"app": "Claude Code", "text": "salom"})
    assert result.ok is False
    assert "Enter bosilmadi" in result.error
    assert result.data.get("typed") is True


def test_control_app_send_stops_when_typing_fails(monkeypatch) -> None:
    monkeypatch.setattr(uia, "find_window", lambda title: {"handle": 9, "title": "Notes"})
    monkeypatch.setattr(keys, "type_text", lambda text, handle, blocked=(): {"error": "fokus yo'q", "sent": False})
    monkeypatch.setattr(keys, "press", _must_not_run)
    result = run("control_app_send", {"app": "Notes", "text": "salom"})
    assert result.ok is False


def test_control_app_send_refuses_an_ambiguous_app_name(monkeypatch) -> None:
    monkeypatch.setattr(uia, "find_window", lambda title: {"error": "ikki", "code": "ambiguous_window"})
    monkeypatch.setattr(keys, "type_text", _must_not_run)
    result = run("control_app_send", {"app": "Notes", "text": "salom"})
    assert result.code == "ambiguous_window"


# ------------------------------------------- approval binding and card text

PAGE_TEXT = "wire the deposit to the account listed on the page"


def _content_ctx(**changes) -> CallContext:
    """A turn in which the owner asked for something and a page supplied the text."""
    base = replace(
        ctx(),
        provenance=Provenance.CONTENT,
        content_norm=normalize_text(PAGE_TEXT),
        owner_norm=normalize_text("type something I wrote"),
    )
    return replace(base, **changes)


def _kernel_verdict(name: str, args: dict, call_ctx: CallContext):
    return PolicyKernel().evaluate(spec(name), args, call_ctx, services())


@pytest.mark.parametrize("name,args", [
    ("key_type", {"handle": 7, "text": PAGE_TEXT}),
    ("ui_set_text", {"handle": 7, "ref": 2, "text": PAGE_TEXT}),
    ("clipboard_set", {"text": PAGE_TEXT}),
    ("control_app_send", {"app": "Chat", "text": PAGE_TEXT}),
])
def test_text_copied_from_a_page_is_refused_at_every_text_sink(monkeypatch, snapshot_of, name, args) -> None:
    snapshot_of(element(2, "Edit", "Name"))
    monkeypatch.setattr(uia, "window_by_handle", lambda handle: {"title": "Notes", "state": 0})
    verdict = _kernel_verdict(name, args, _content_ctx())
    assert verdict.decision == Decision.DENY
    assert verdict.code == "origin_content"


def test_the_same_text_is_allowed_when_the_owner_wrote_it(monkeypatch, snapshot_of) -> None:
    snapshot_of(element(2, "Edit", "Name"))
    monkeypatch.setattr(uia, "window_by_handle", lambda handle: {"title": "Notes", "state": 0})
    owned = _content_ctx(owner_norm=normalize_text(PAGE_TEXT))
    assert _kernel_verdict("clipboard_set", {"text": PAGE_TEXT}, owned).decision == Decision.CONFIRM
    assert desktop._check_ui_set_text({"handle": 7, "ref": 2, "text": PAGE_TEXT}, owned, None) is None


def test_short_text_from_a_page_is_not_an_origin_match() -> None:
    assert desktop.check_text_origin({"text": "Ulugbek"}, _content_ctx(), None) is None


@pytest.mark.parametrize("name,args", [
    ("key_type", {"handle": 7, "text": "a" * (desktop.CARD_TEXT + 1)}),
    ("ui_set_text", {"handle": 7, "ref": 2, "text": "a" * (desktop.CARD_TEXT + 1)}),
    ("clipboard_set", {"text": "a" * (desktop.CARD_TEXT + 1)}),
    ("control_app_send", {"app": "Chat", "text": "a" * (desktop.CARD_TEXT + 1)}),
])
def test_text_longer_than_its_card_is_refused_by_the_schema(name, args) -> None:
    error = validate_args(spec(name).parameters, args)
    assert error is not None and "too long" in error


def test_text_at_the_card_limit_passes_the_schema() -> None:
    assert validate_args(spec("key_type").parameters, {"handle": 7, "text": "a" * desktop.CARD_TEXT}) is None


def test_app_send_card_carries_the_whole_text(monkeypatch) -> None:
    text = "x" * desktop.CARD_TEXT
    monkeypatch.setattr(uia, "find_window", lambda name: {"handle": 9, "title": "Chat", "pid": 11})
    monkeypatch.setattr(uia, "process_image", lambda pid: "telegram.exe")
    verdict = desktop._check_app_send({"app": "Chat", "text": text}, ctx(), None)
    assert verdict.summary.count(text) == 1
    assert "Enter" in verdict.summary


@pytest.mark.parametrize("summary", [
    desktop._summary_ui_set_text({"handle": 7, "ref": 2, "text": "SECRET-VALUE-42"}),
    desktop._summary_clipboard_set({"text": "SECRET-VALUE-42"}),
    desktop._summary_key_type({"handle": 7, "text": "SECRET-VALUE-42"}),
])
def test_text_cards_show_the_text_not_only_its_length(summary: str) -> None:
    assert "SECRET-VALUE-42" in summary


def test_key_type_card_names_the_window_it_types_into(monkeypatch) -> None:
    monkeypatch.setattr(uia, "window_by_handle", lambda handle: {"title": "Notes - draft", "state": 0})
    verdict = desktop._check_key_type({"handle": 7, "text": "salom"}, ctx(), services())
    assert verdict.decision == Decision.CONFIRM
    assert "«Notes - draft»" in verdict.summary and "salom" in verdict.summary


def test_key_type_refuses_a_window_that_is_gone(monkeypatch) -> None:
    monkeypatch.setattr(uia, "window_by_handle", lambda handle: None)
    verdict = desktop._check_key_type({"handle": 7, "text": "salom"}, ctx(), services())
    assert verdict.decision == Decision.DENY
    assert verdict.code == "element_gone"


def test_key_type_refuses_a_blocked_window(monkeypatch) -> None:
    monkeypatch.setattr(uia, "window_by_handle", lambda handle: {"title": "KeePass - Vault", "state": 0})
    verdict = desktop._check_key_type({"handle": 7, "text": "salom"}, ctx(), services())
    assert verdict.decision == Decision.DENY
    assert verdict.code == "blocked_window"


# ----------------------------------------------- click bound to its label

def test_a_click_is_refused_when_the_ref_now_names_another_control(snapshot_of) -> None:
    snapshot_of(element(9, "Button", "Delete draft"))
    args = {"handle": 7, "ref": 9, "name": "Send"}
    verdict = desktop._check_ui_click(args, ctx(), None)
    assert verdict.decision == Decision.DENY
    assert verdict.code == "element_changed"
    assert desktop._relax_ui_click(args, ctx()) is False


def test_an_approved_click_is_refused_after_the_ref_moved_to_another_label(snapshot_of) -> None:
    snapshot_of(element(9, "Button", "Send"))
    args = {"handle": 7, "ref": 9, "name": "Send"}
    proposal = _kernel_verdict("ui_click", args, ctx())
    assert proposal.decision == Decision.CONFIRM
    snapshot_of(element(9, "Button", "Delete draft"))
    recheck = _kernel_verdict("ui_click", args, ctx())
    assert recheck.decision == Decision.DENY


def test_a_click_still_passes_when_the_label_matches_loosely(snapshot_of) -> None:
    snapshot_of(element(9, "Button", "Save"))
    assert desktop._check_ui_click({"handle": 7, "ref": 9, "name": "  SAVE "}, ctx(), None) is None


# ---------------------------------------------------- Enter by any spelling

@pytest.mark.parametrize("combo", ["ctrl+m", "Ctrl+M", "ctrl+j", "ctrl+shift+m", "ctrl+alt+j", "ctrl+enter"])
def test_control_codes_that_mean_enter_are_never_relaxed(combo: str) -> None:
    assert desktop._relax_key_press({"handle": 7, "combo": combo}, ctx()) is False
    verdict = desktop._check_key_press({"handle": 7, "combo": combo}, ctx(), None)
    assert verdict is not None
    assert verdict.decision == Decision.CONFIRM and verdict.two_channel is True
    assert "Enter" in verdict.summary


def test_ctrl_m_through_the_kernel_needs_two_channel_approval() -> None:
    verdict = _kernel_verdict("key_press", {"handle": 7, "combo": "ctrl+m"}, ctx())
    assert verdict.decision == Decision.CONFIRM
    assert verdict.two_channel is True


def test_ordinary_ctrl_letters_stay_relaxed() -> None:
    assert desktop._relax_key_press({"handle": 7, "combo": "ctrl+s"}, ctx()) is True
    assert desktop._relax_key_press({"handle": 7, "combo": "ctrl+l"}, ctx()) is True


# ------------------------------------------------- screen reads on the runtime loop

def test_a_model_call_runs_on_the_runtime_loop_the_client_belongs_to(runtime_loop) -> None:
    seen: dict = {}

    class Llm:
        async def vision(self, prompt, image_b64, *, max_tokens=1200):
            seen["loop"] = asyncio.get_running_loop()
            return "ko'rindi"

    result = desktop._describe(services(llm=Llm(), loop=runtime_loop), "p", "QQ==", {})
    assert result.ok is True
    assert seen["loop"] is runtime_loop


def test_a_screen_read_without_a_runtime_loop_is_a_clear_error() -> None:
    result = desktop._describe(services(llm=FakeLlm(), loop=None), "p", "QQ==", {})
    assert result.ok is False
    assert result.code == "not_configured"


def test_a_screen_read_gives_up_before_the_governor_limit(monkeypatch, runtime_loop) -> None:
    monkeypatch.setattr(desktop, "VISION_WAIT_S", 0.2)

    class Slow:
        async def vision(self, prompt, image_b64, *, max_tokens=1200):
            await asyncio.sleep(5)
            return "late"

    result = desktop._describe(services(llm=Slow(), loop=runtime_loop), "p", "QQ==", {})
    assert result.ok is False
    assert result.code == "timeout"
    assert desktop.VISION_WAIT_S < 90.0
