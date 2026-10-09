"""UI Automation layer: names, exact matching, ambiguity, passwords, focus guards and the abandon rule.

The tree is a fake. A small stand-in for the uiautomation module is installed in
sys.modules, so the code under test runs its real job functions on the real UIA
worker thread without touching a desktop. Pure helpers are tested directly.
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

from coworker import uia
from coworker.uia import Element, Snapshot

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="needs the Windows user32 API")


# ---------------------------------------------------------------- fake tree

class FakeRect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left, self.top, self.right, self.bottom = left, top, right, bottom

    def width(self) -> int:
        return self.right - self.left

    def height(self) -> int:
        return self.bottom - self.top


class FakeValue:
    def __init__(self, control: "FakeControl") -> None:
        self._control = control

    @property
    def Value(self) -> str:
        return self._control.value

    def SetValue(self, text: str) -> None:
        self._control.typed.append(text)


class FakeWindowPattern:
    def __init__(self, control: "FakeControl") -> None:
        self._control = control
        self.WindowVisualState = 0

    def Close(self) -> None:
        self._control.closed += 1

    def SetWindowVisualState(self, state: int) -> None:
        self.WindowVisualState = state


class FakeInvoke:
    def __init__(self, control: "FakeControl") -> None:
        self._control = control

    def Invoke(self) -> None:
        self._control.invoked += 1


class FakeControl:
    def __init__(
        self,
        name: str = "",
        kind: str = "Button",
        handle: int = 0,
        children: tuple = (),
        rect: tuple = (0, 0, 100, 40),
        class_name: str = "",
        is_password: bool = False,
        value: str = "",
        invokable: bool = True,
        top_handle: int | None = None,
    ) -> None:
        self.ControlTypeName = f"{kind}Control"
        self.Name = name
        self.NativeWindowHandle = handle
        self.ClassName = class_name
        self.ProcessId = 4242
        self.BoundingRectangle = FakeRect(*rect)
        self.IsPassword = is_password
        self.IsEnabled = True
        self.HasKeyboardFocus = False
        self.value = value
        self.typed: list[str] = []
        self.invoked = 0
        self.closed = 0
        self.clicked = 0
        self.sent_keys: list[str] = []
        self._children = list(children)
        self._invokable = invokable
        self._top_handle = top_handle
        self._window_pattern = FakeWindowPattern(self)

    def GetChildren(self) -> list:
        return list(self._children)

    def GetWindowPattern(self) -> FakeWindowPattern:
        return self._window_pattern

    def GetValuePattern(self) -> FakeValue:
        return FakeValue(self)

    def GetInvokePattern(self):
        return FakeInvoke(self) if self._invokable else None

    def GetTogglePattern(self):
        return None

    def GetTopLevelControl(self):
        return FakeControl(handle=self._top_handle) if self._top_handle is not None else None

    def SetFocus(self) -> None:
        pass

    def SendKeys(self, text: str, waitTime: float = 0) -> None:
        self.sent_keys.append(text)

    def Click(self, simulateMove: bool = True, **_: object) -> None:
        self.clicked += 1


class FakeKeys:
    """Attribute access returns the constant's name, which is all the code under test needs."""

    def __getattr__(self, name: str) -> str:
        if name.startswith("VK_"):
            return name
        raise AttributeError(name)


class FakeAuto:
    Keys = FakeKeys()

    def __init__(self, windows: list[FakeControl], hit: FakeControl | None = None) -> None:
        self._root = FakeControl(name="Desktop", kind="Pane", children=windows)
        self._hit = hit
        self.focused: FakeControl | None = None
        self.focus_error: Exception | None = None

    def GetRootControl(self) -> FakeControl:
        return self._root

    def GetFocusedControl(self) -> FakeControl | None:
        if self.focus_error is not None:
            raise self.focus_error
        return self.focused

    def SetGlobalSearchTimeout(self, seconds: float) -> None:
        pass

    def ControlFromPoint(self, x: int, y: int):
        return self._hit

    def ControlFromHandle(self, handle: int):
        return next((w for w in self._root.GetChildren() if w.NativeWindowHandle == handle), None)


def window(title: str, handle: int, children: tuple = (), class_name: str = "") -> FakeControl:
    return FakeControl(name=title, kind="Window", handle=handle, children=children,
                       rect=(0, 0, 800, 600), class_name=class_name)


@pytest.fixture
def auto(monkeypatch):
    """Install a fake uiautomation with an empty desktop; tests replace the children."""
    fake = FakeAuto([])
    monkeypatch.setitem(sys.modules, "uiautomation", fake)
    monkeypatch.setattr(uia, "available", lambda: True)
    monkeypatch.setattr(uia, "_snapshots", {})
    return fake


def _set_windows(auto: FakeAuto, *windows: FakeControl) -> None:
    auto._root = FakeControl(name="Desktop", kind="Pane", children=list(windows))


# ------------------------------------------------------------- pure helpers

def test_clean_removes_private_use_glyphs_and_keeps_hyphens() -> None:
    assert uia._clean(" Save-As\U000f0001 Ctrl-S") == "Save-As Ctrl-S"


def test_clean_collapses_whitespace_and_handles_empty() -> None:
    assert uia._clean("Hello \n  world") == "Hello world"
    assert uia._clean(None) == ""
    assert uia._clean("") == ""


def test_pick_window_matches_the_full_name_only() -> None:
    assert uia.pick_window(["Notes Pro", "Notes"], "Notes") == (1, [])
    assert uia.pick_window(["Notes Pro"], "Notes") == (None, [])
    assert uia.pick_window(["Notes Pro"], "") == (None, [])


def test_pick_window_folds_case_and_apostrophes() -> None:
    assert uia.pick_window(["Don’t close"], "don't CLOSE") == (0, [])


def test_pick_window_refuses_a_tie_and_returns_every_tied_index() -> None:
    index, tied = uia.pick_window(["Notes", "notes ", "Other"], "NOTES")
    assert index is None
    assert tied == [0, 1]


def test_similar_titles_are_hints_for_a_missed_name() -> None:
    assert uia.similar_titles(["Notes Pro", "Other"], "notes") == ["Notes Pro"]
    assert uia.similar_titles(["Notes Pro"], "") == []


def test_blocked_window_matches_title_or_class_case_folded() -> None:
    patterns = uia.DEFAULT_BLOCKED_WINDOWS
    assert uia.is_blocked_window("KeePassXC - Database", "Qt5QWindowIcon", patterns)
    assert uia.is_blocked_window("Notes", "BITWARDEN_CLASS", patterns)
    assert not uia.is_blocked_window("Notes", "Notepad", patterns)
    assert not uia.is_blocked_window("Notes", "Notepad", ("",))


def test_element_line_never_shows_a_password_value() -> None:
    element = Element(ref=1, type="Edit", name="Password", path=(), value="hunter2", is_password=True)
    text = element.line()
    assert "hunter2" not in text
    assert "parol" in text


def test_snapshot_api_returns_the_cached_read(monkeypatch) -> None:
    element = Element(ref=1, type="Button", name="OK", path=(0,))
    monkeypatch.setattr(uia, "_snapshots", {7: Snapshot(handle=7, elements=[element])})
    assert uia.snapshot(7).by_ref(1).name == "OK"
    assert uia.snapshot(8) is None


# ----------------------------------------------------------- window reading

def test_read_window_refuses_an_ambiguous_title_and_lists_candidates(auto) -> None:
    _set_windows(auto, window("Notes", 10), window("notes", 20))
    result = uia.read_window(title="notes")
    assert result["code"] == "ambiguous_window"
    assert sorted(c["handle"] for c in result["candidates"]) == [10, 20]
    assert uia._snapshots == {}


def test_read_window_by_exact_title_and_by_handle(auto) -> None:
    ok_button = FakeControl(name="OK", kind="Button", rect=(10, 10, 80, 30))
    _set_windows(auto, window("Notes Pro", 30, children=(ok_button,)), window("Notes", 31))
    by_title = uia.read_window(title="Notes Pro")
    assert by_title["handle"] == 30
    assert 'Button "OK"' in by_title["elements"]
    by_handle = uia.read_window(handle=30)
    assert by_handle["window"] == "Notes Pro"


def test_read_window_refuses_a_blocked_window_before_reading_it(auto) -> None:
    child = FakeControl(name="Entry", kind="Button")
    _set_windows(auto, window("KeePass - Vault", 40, children=(child,)))
    result = uia.read_window(handle=40)
    assert result["code"] == "blocked_window"
    assert "elements" not in result


def test_element_names_are_cleaned_in_the_snapshot(auto) -> None:
    save = FakeControl(name=" Save-As", kind="Button", rect=(0, 0, 50, 20))
    _set_windows(auto, window("Notes", 50, children=(save,)))
    uia.read_window(handle=50)
    assert uia.snapshot(50).by_ref(1).name == "Save-As"


def test_password_field_is_flagged_and_its_value_is_not_read(auto) -> None:
    field = FakeControl(name="Password", kind="Edit", is_password=True, value="s3cret",
                        rect=(0, 0, 200, 20))
    _set_windows(auto, window("Login", 60, children=(field,)))
    result = uia.read_window(handle=60)
    element = uia.snapshot(60).by_ref(1)
    assert element.is_password is True
    assert element.value == ""
    assert "s3cret" not in result["elements"]


def test_list_windows_returns_full_titles_and_handles(auto) -> None:
    long_title = "x" * 150
    _set_windows(auto, window(long_title, 70))
    result = uia.list_windows()
    assert result["count"] == 1
    assert result["windows"][0]["title"] == long_title
    assert result["windows"][0]["handle"] == 70


def test_find_window_is_exact_and_hints_on_a_miss(auto) -> None:
    _set_windows(auto, window("Notes", 80), window("Notes Pro", 81))
    assert uia.find_window("Notes") == {"handle": 80, "title": "Notes", "pid": 4242}
    missed = uia.find_window("Note")
    assert missed["error"]
    assert missed["similar"] == ["Notes", "Notes Pro"]


# -------------------------------------------------------------- acting

def test_click_uses_the_invoke_pattern(auto) -> None:
    button = FakeControl(name="Save", kind="Button", rect=(0, 0, 50, 20))
    _set_windows(auto, window("Notes", 90, children=(button,)))
    uia.read_window(handle=90)
    result = uia.click(90, 1)
    assert result["ok"] is True
    assert button.invoked == 1


def test_click_with_a_stale_ref_is_refused(auto) -> None:
    result = uia.click(999, 1)
    assert result["code"] == "element_gone"


def test_set_text_refuses_a_password_field(auto) -> None:
    field = FakeControl(name="Password", kind="Edit", is_password=True, rect=(0, 0, 200, 20))
    _set_windows(auto, window("Login", 100, children=(field,)))
    uia.read_window(handle=100)
    result = uia.set_text(100, 1, "new")
    assert result["code"] == "password_field"
    assert field.typed == []


def test_pointer_fallback_clicks_only_when_the_target_is_in_front(auto, monkeypatch) -> None:
    stubborn = FakeControl(name="Ok", kind="Button", rect=(0, 0, 50, 20), invokable=False)
    _set_windows(auto, window("Dialog", 110, children=(stubborn,)))
    uia.read_window(handle=110)
    monkeypatch.setattr(uia, "foreground_handle", lambda: 110)
    auto._hit = FakeControl(handle=0, top_handle=110)
    assert uia.click(110, 1)["via"] == "sichqoncha"
    assert stubborn.clicked == 1


def test_pointer_fallback_refuses_when_another_window_is_on_top(auto, monkeypatch) -> None:
    stubborn = FakeControl(name="Ok", kind="Button", rect=(0, 0, 50, 20), invokable=False)
    _set_windows(auto, window("Dialog", 120, children=(stubborn,)))
    uia.read_window(handle=120)
    monkeypatch.setattr(uia, "foreground_handle", lambda: 120)
    auto._hit = FakeControl(handle=0, top_handle=999)
    result = uia.click(120, 1)
    assert "error" in result
    assert stubborn.clicked == 0


def test_ensure_foreground_refuses_handle_zero() -> None:
    result = uia.ensure_foreground(0)
    assert result["code"] == "arg_invalid"
    assert result["sent"] is False


def test_ensure_foreground_refuses_a_blocked_window_even_when_in_front(auto, monkeypatch) -> None:
    _set_windows(auto, window("KeePass", 130))
    monkeypatch.setattr(uia, "foreground_handle", lambda: 130)
    result = uia.ensure_foreground(130)
    assert result["code"] == "blocked_window"
    assert result["sent"] is False


def test_ensure_foreground_is_quiet_when_the_target_already_has_focus(auto, monkeypatch) -> None:
    _set_windows(auto, window("Notes", 140))
    monkeypatch.setattr(uia, "foreground_handle", lambda: 140)
    assert uia.ensure_foreground(140) is None


def test_ensure_foreground_brings_the_window_forward_and_checks_it(auto, monkeypatch) -> None:
    _set_windows(auto, window("Notes", 150))
    monkeypatch.setattr(uia, "foreground_handle", lambda: 1)
    monkeypatch.setattr(uia, "_force_foreground", lambda handle: True)
    assert uia.ensure_foreground(150) is None


def test_ensure_foreground_reports_failure_with_sent_false(auto, monkeypatch) -> None:
    _set_windows(auto, window("Notes", 160))
    monkeypatch.setattr(uia, "foreground_handle", lambda: 1)
    monkeypatch.setattr(uia, "_force_foreground", lambda handle: False)
    result = uia.ensure_foreground(160)
    assert result["sent"] is False
    assert "error" in result


def test_find_window_never_returns_handle_zero(auto) -> None:
    """Handle 0 would make a screenshot capture the whole screen, so it is refused here."""
    _set_windows(auto, window("Notes", 0))
    result = uia.find_window("Notes")
    assert result["code"] == "element_gone"
    assert "handle" not in result


def test_close_window_reports_close_requested_never_closed(auto) -> None:
    _set_windows(auto, window("Notes", 170))
    result = uia.close_window(170)
    assert result == {"ok": True, "close_requested": "Notes"}
    assert "closed" not in result


# ------------------------------------------------ abandon-and-count rule

def test_worker_abandons_slow_jobs_and_disables_after_three(monkeypatch) -> None:
    worker = uia._Worker()
    release = threading.Event()
    ran_after_disable: list[bool] = []

    def stuck():
        release.wait(5)
        return "late"

    try:
        for _ in range(uia.MAX_ABANDONED):
            with pytest.raises(uia.JobAbandoned):
                worker.call(stuck, timeout=0.05)

        def never():
            ran_after_disable.append(True)

        with pytest.raises(uia.PoolDisabled):
            worker.call(never, timeout=0.05)
        assert ran_after_disable == []
    finally:
        release.set()


def test_run_maps_an_abandoned_job_to_an_unknown_outcome(monkeypatch) -> None:
    worker = uia._Worker()
    release = threading.Event()
    monkeypatch.setattr(uia, "_worker", worker)
    try:
        result = uia.run(lambda: release.wait(5), timeout=0.05)
        assert result["code"] == "timeout"
        assert result["outcome"] == "unknown"
    finally:
        release.set()


def test_run_maps_a_disabled_pool_to_throttled(monkeypatch) -> None:
    worker = uia._Worker()
    release = threading.Event()
    monkeypatch.setattr(uia, "_worker", worker)
    try:
        for _ in range(uia.MAX_ABANDONED):
            uia.run(lambda: release.wait(5), timeout=0.05)
        result = uia.run(lambda: "fast", timeout=0.5)
        assert result["code"] == "throttled"
    finally:
        release.set()


def test_run_returns_the_job_result_and_reports_job_errors(monkeypatch) -> None:
    monkeypatch.setattr(uia, "_worker", uia._Worker())
    assert uia.run(lambda: {"ok": 1}) == {"ok": 1}

    def broken():
        raise ValueError("bad state")

    assert "bad state" in uia.run(broken)["error"]


# ------------------------------------------------------------- Windows only

@pytest.mark.windows
@WINDOWS_ONLY
def test_real_worker_runs_jobs_on_the_uia_thread() -> None:
    assert uia.run(lambda: threading.current_thread().name, timeout=5) == "uia"


@pytest.mark.windows
@WINDOWS_ONLY
def test_real_foreground_handle_is_a_non_negative_integer() -> None:
    handle = uia.foreground_handle()
    assert isinstance(handle, int) and handle >= 0


# ------------------------------------------- focus and label binding

def test_a_password_field_with_focus_refuses_the_send(auto) -> None:
    auto.focused = FakeControl(name="Password", kind="Edit", is_password=True)
    refused = uia.ensure_not_password_focus(auto)
    assert refused["code"] == "password_field"
    assert refused["sent"] is False


def test_an_ordinary_focused_control_may_receive_keys(auto) -> None:
    auto.focused = FakeControl(name="Message", kind="Edit", is_password=False)
    assert uia.ensure_not_password_focus(auto) is None


def test_no_focused_control_at_all_may_receive_keys(auto) -> None:
    auto.focused = None
    assert uia.ensure_not_password_focus(auto) is None


def test_an_unreadable_focus_sends_nothing(auto) -> None:
    auto.focus_error = RuntimeError("UIA has no focus answer")
    refused = uia.ensure_not_password_focus(auto)
    assert refused["code"] == "focus_unknown"
    assert refused["sent"] is False


def test_a_control_without_the_password_property_is_not_a_password(auto) -> None:
    class Bare:
        pass

    auto.focused = Bare()
    assert uia.ensure_not_password_focus(auto) is None


def test_click_refuses_when_the_ref_names_another_label(monkeypatch) -> None:
    monkeypatch.setattr(uia, "available", lambda: True)
    monkeypatch.setattr(uia, "_act", _must_not_act)
    monkeypatch.setattr(uia, "_snapshots", {7: Snapshot(handle=7, elements=[Element(
        ref=9, type="Button", name="Delete draft", path=(0,))])})
    result = uia.click(7, 9, expect_name="Send")
    assert result["code"] == "element_gone"
    assert "Send" in result["error"]


def test_click_proceeds_when_the_label_is_the_one_the_owner_saw(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(uia, "available", lambda: True)
    monkeypatch.setattr(uia, "_act", lambda handle, element, action, blocked: seen.update(name=element.name) or {"ok": True})
    monkeypatch.setattr(uia, "_snapshots", {7: Snapshot(handle=7, elements=[Element(
        ref=9, type="Button", name="Send", path=(0,))])})
    assert uia.click(7, 9, expect_name=" send ")["ok"] is True
    assert seen["name"] == "Send"


def test_click_without_an_expected_label_keeps_the_old_behaviour(monkeypatch) -> None:
    monkeypatch.setattr(uia, "available", lambda: True)
    monkeypatch.setattr(uia, "_act", lambda handle, element, action, blocked: {"ok": True, "name": element.name})
    monkeypatch.setattr(uia, "_snapshots", {7: Snapshot(handle=7, elements=[Element(
        ref=9, type="Button", name="Save", path=(0,))])})
    assert uia.click(7, 9)["name"] == "Save"


def _must_not_act(*args, **kwargs):
    raise AssertionError("the click must not reach the UIA job")


# ------------------------------------------------------ owning program

def test_process_image_of_no_process_is_unknown() -> None:
    assert uia.process_image(0) == ""


def test_process_image_is_unknown_off_windows(monkeypatch) -> None:
    import types

    monkeypatch.setattr(uia, "os", types.SimpleNamespace(name="posix", path=os.path))
    assert uia.process_image(1234) == ""


@WINDOWS_ONLY
def test_process_image_names_this_test_process_on_windows() -> None:
    name = uia.process_image(os.getpid())
    assert name.endswith(".exe")
    assert name == name.lower()
