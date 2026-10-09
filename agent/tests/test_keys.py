"""Keystrokes and the clipboard: combo parsing, focus checks inside the send job, modifier release, clipboard round trips.

The clipboard is always a FakeBoard and uiautomation is a fake, so no test
touches the real clipboard, a real window or the keyboard. Sleeps are patched
out so the paste timing does not slow the suite down.
"""
from __future__ import annotations

import os
import sys
import threading
import types

import pytest

from coworker import keys, uia

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="needs the Windows clipboard API")

CF_UNICODE = keys.CF_UNICODETEXT
CF_DIB = 8
CF_BITMAP = 2
PRIVATE_FORMAT = 0xC001


# ---------------------------------------------------------------- fakes

class FakeBoard:
    """A clipboard held as a list of (format, bytes). Formats in `unavailable` have no data."""

    def __init__(self, items=(), *, can_open: bool = True, unavailable: tuple = ()) -> None:
        self.items: list[tuple[int, bytes]] = list(items)
        self.can_open = can_open
        self.unavailable = set(unavailable)
        self.opened = False
        self.writes: list[tuple[int, bytes]] = []

    def open(self) -> bool:
        if self.can_open:
            self.opened = True
        return self.can_open

    def close(self) -> None:
        self.opened = False

    def formats(self) -> list[int]:
        assert self.opened, "clipboard used while closed"
        return [fmt for fmt, _ in self.items]

    def size(self, fmt: int):
        assert self.opened
        if fmt in self.unavailable:
            return None
        return len(dict(self.items)[fmt])

    def read(self, fmt: int):
        assert self.opened
        if fmt in self.unavailable:
            return None
        return dict(self.items).get(fmt)

    def clear(self) -> None:
        assert self.opened
        self.items = []

    def write(self, fmt: int, data: bytes) -> bool:
        assert self.opened
        self.items.append((fmt, data))
        self.writes.append((fmt, data))
        return True


class _KeyNames:
    """Attribute access returns the constant's name, which is all the code under test needs."""

    def __getattr__(self, name: str) -> str:
        if name.startswith("VK_"):
            return name
        raise AttributeError(name)


class _Pattern:
    WindowVisualState = 0

    def SetWindowVisualState(self, state: int) -> None:
        pass


class FakeWindow:
    ControlTypeName = "WindowControl"
    ClassName = ""
    ProcessId = 1

    def __init__(self, title: str, handle: int) -> None:
        self.Name = title
        self.NativeWindowHandle = handle
        self.BoundingRectangle = types.SimpleNamespace(
            left=0, top=0, right=10, bottom=10, width=lambda: 10, height=lambda: 10)

    def GetWindowPattern(self) -> _Pattern:
        return _Pattern()

    def SetFocus(self) -> None:
        pass

    def GetChildren(self) -> list:
        return []


class FakeFocus:
    """The control that holds the keyboard focus. Only IsPassword matters to the code under test."""

    def __init__(self, is_password: bool = False) -> None:
        self.IsPassword = is_password


class FakeAuto:
    """A uiautomation stand-in: a desktop of windows, a focused control, and recorded key presses."""

    Keys = _KeyNames()

    def __init__(self, windows: list) -> None:
        self.windows = windows
        self.events: list[tuple[str, str]] = []
        self.fail_on: set[str] = set()
        self.focused: FakeFocus | None = FakeFocus(False)
        self.focus_error: Exception | None = None

    def GetFocusedControl(self):
        if self.focus_error is not None:
            raise self.focus_error
        return self.focused

    def GetRootControl(self) -> "FakeAuto":
        return self

    def GetChildren(self) -> list:
        return list(self.windows)

    def SetGlobalSearchTimeout(self, seconds: float) -> None:
        pass

    def ControlFromHandle(self, handle: int):
        return next((w for w in self.windows if w.NativeWindowHandle == handle), None)

    def PressKey(self, code: str) -> None:
        if code in self.fail_on:
            raise RuntimeError(f"press failed: {code}")
        self.events.append(("press", code))

    def ReleaseKey(self, code: str) -> None:
        self.events.append(("release", code))


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-le") + b"\x00\x00"


@pytest.fixture
def env(monkeypatch):
    """A fake uiautomation with window 7 ("Notes") in front, a clipboard board, and no sleeps."""
    fake = FakeAuto([FakeWindow("Notes", 7)])
    monkeypatch.setitem(sys.modules, "uiautomation", fake)
    monkeypatch.setattr(keys, "available", lambda: True)
    monkeypatch.setattr(uia, "available", lambda: True)
    monkeypatch.setattr(uia, "foreground_handle", lambda: 7)
    monkeypatch.setattr(keys, "time", types.SimpleNamespace(sleep=lambda seconds: None))
    board = FakeBoard([(CF_UNICODE, _utf16("old")), (PRIVATE_FORMAT, b"private")])
    monkeypatch.setattr(keys, "_board", lambda: board)
    return types.SimpleNamespace(auto=fake, board=board, monkeypatch=monkeypatch)


def _presses(auto: FakeAuto) -> list[str]:
    return [code for kind, code in auto.events if kind == "press"]


def _releases(auto: FakeAuto) -> list[str]:
    return [code for kind, code in auto.events if kind == "release"]


# ------------------------------------------------------------ combinations

def test_parse_combo_canonicalises_modifiers_and_key_aliases() -> None:
    assert keys.parse_combo("Control + Shift + S") == (("ctrl", "shift"), "s")
    assert keys.parse_combo("cmd+return") == (("win",), "enter")
    assert keys.parse_combo("kirit") == ((), "enter")
    assert keys.parse_combo("ochir") == ((), "delete")
    assert keys.parse_combo("alt+F4") == (("alt",), "f4")
    assert keys.parse_combo("Ctrl+Ctrl+c") == (("ctrl",), "c")


def test_parse_combo_orders_modifiers_the_same_way_every_time() -> None:
    assert keys.parse_combo("win+shift+alt+ctrl+x") == (("ctrl", "alt", "shift", "win"), "x")


@pytest.mark.parametrize("combo", ["", "+", "ctrl", "a+b", "ctrl+xyz", "f25", "ctrl+%"])
def test_parse_combo_refuses_incomplete_or_unknown_combinations(combo: str) -> None:
    with pytest.raises(ValueError):
        keys.parse_combo(combo)


def test_normalize_gives_one_spelling_per_combination() -> None:
    assert keys.normalize("shift+ctrl+s") == "ctrl+shift+s"
    assert keys.normalize("Win + Alt + Tab") == "alt+win+tab"


def test_dangerous_combinations_include_closing_and_deleting() -> None:
    assert keys.is_dangerous("alt+F4")
    assert keys.is_dangerous("ctrl+w")
    assert keys.is_dangerous("ctrl+f4")
    assert keys.is_dangerous("shift+delete")
    assert not keys.is_dangerous("ctrl+s")


def test_an_unparseable_combination_counts_as_dangerous() -> None:
    assert keys.is_dangerous("ctrl+%") is True


# ---------------------------------------------------------------- refusals

def _must_not_run(*args, **kwargs):
    raise AssertionError("no UIA job may run for this call")


def test_key_actions_refuse_handle_zero_before_any_worker_call(monkeypatch) -> None:
    monkeypatch.setattr(keys, "available", lambda: True)
    monkeypatch.setattr(uia, "run", _must_not_run)
    assert keys.press("ctrl+s", 0)["code"] == "arg_invalid"
    assert keys.type_text("hello", 0)["code"] == "arg_invalid"


def test_type_text_refuses_text_above_the_cap_and_never_truncates(monkeypatch) -> None:
    monkeypatch.setattr(keys, "available", lambda: True)
    monkeypatch.setattr(uia, "run", _must_not_run)
    result = keys.type_text("a" * (keys.MAX_TEXT + 1), 7)
    assert result["code"] == "arg_invalid"
    assert str(keys.MAX_TEXT) in result["error"]


def test_type_text_accepts_text_exactly_at_the_cap(monkeypatch) -> None:
    ran: list[bool] = []

    def fake_run(fn, timeout=0):
        ran.append(True)
        return {"ok": True}

    monkeypatch.setattr(keys, "available", lambda: True)
    monkeypatch.setattr(uia, "run", fake_run)
    assert keys.type_text("a" * keys.MAX_TEXT, 7) == {"ok": True}
    assert ran == [True]


def test_clipboard_set_refuses_text_above_the_cap(monkeypatch) -> None:
    monkeypatch.setattr(keys, "available", lambda: True)
    monkeypatch.setattr(uia, "run", _must_not_run)
    assert keys.clipboard_set("a" * (keys.MAX_TEXT + 1))["code"] == "arg_invalid"


# ------------------------------------------------- keystroke order and focus

def test_press_sends_the_chord_in_order_and_releases_in_reverse(env) -> None:
    result = keys.press("ctrl+shift+s", 7)
    assert result["ok"] is True
    assert result["pressed"] == "ctrl+shift+s"
    assert _presses(env.auto) == ["VK_CONTROL", "VK_SHIFT", "VK_S"]
    assert _releases(env.auto) == ["VK_S", "VK_SHIFT", "VK_CONTROL"]


def test_press_releases_every_key_it_pressed_when_the_main_key_fails(env) -> None:
    env.auto.fail_on.add("VK_P")
    result = keys.press("ctrl+shift+p", 7)
    assert "error" in result
    assert _releases(env.auto) == ["VK_SHIFT", "VK_CONTROL"]


def test_press_verifies_focus_inside_the_same_job_and_sends_nothing_when_refused(env) -> None:
    env.monkeypatch.setattr(uia, "foreground_handle", lambda: 3)
    env.monkeypatch.setattr(uia, "_force_foreground", lambda handle: False)
    result = keys.press("ctrl+s", 7)
    assert result["sent"] is False
    assert env.auto.events == []


def test_focus_check_runs_on_the_uia_thread_inside_the_job(env) -> None:
    threads: list[str] = []

    def record(handle, blocked=uia.DEFAULT_BLOCKED_WINDOWS):
        threads.append(threading.current_thread().name)
        return None

    env.monkeypatch.setattr(uia, "ensure_foreground", record)
    keys.press("enter", 7)
    assert threads == ["uia"]


def test_press_refuses_a_blocked_window_even_when_it_has_focus(env) -> None:
    env.auto.windows = [FakeWindow("KeePass", 7)]
    result = keys.press("ctrl+s", 7)
    assert result["code"] == "blocked_window"
    assert env.auto.events == []


def test_press_reports_the_window_where_the_keys_landed(env) -> None:
    assert keys.press("f5", 7)["window"] == "Notes"


def test_press_refuses_when_the_focused_control_is_a_password_field(env) -> None:
    env.auto.focused = FakeFocus(is_password=True)
    result = keys.press("ctrl+v", 7)
    assert result["code"] == "password_field"
    assert result["sent"] is False
    assert env.auto.events == []


def test_type_text_never_pastes_into_a_password_field(env) -> None:
    env.auto.focused = FakeFocus(is_password=True)
    result = keys.type_text("hunter2-secret", 7)
    assert result["code"] == "password_field"
    assert result["sent"] is False
    assert env.board.writes == []
    assert env.auto.events == []


def test_type_text_pastes_into_an_ordinary_focused_field(env) -> None:
    env.auto.focused = FakeFocus(is_password=False)
    assert keys.type_text("salom", 7)["ok"] is True


def test_type_text_sends_nothing_when_the_focus_cannot_be_read(env) -> None:
    env.auto.focus_error = RuntimeError("no focus information")
    result = keys.type_text("salom", 7)
    assert result["code"] == "focus_unknown"
    assert env.board.writes == []
    assert env.auto.events == []


def test_the_focus_check_runs_after_the_window_check_and_before_the_send(env) -> None:
    order: list[str] = []
    env.monkeypatch.setattr(uia, "ensure_foreground", lambda handle, blocked=(): order.append("foreground") or None)
    env.monkeypatch.setattr(uia, "ensure_not_password_focus",
                            lambda auto: order.append("focus") or None)
    env.monkeypatch.setattr(keys, "_paste", lambda auto, text: order.append("send") or {"ok": True})
    keys.type_text("salom", 7)
    assert order == ["foreground", "focus", "send"]


# ------------------------------------------- Enter by any spelling

@pytest.mark.parametrize("combo", [
    "enter", "Return", "kirit", "shift+enter", "ctrl+enter",
    "ctrl+m", "Ctrl+M", "ctrl+j", "ctrl+shift+m", "control+j",
])
def test_sends_enter_covers_enter_and_the_control_codes_of_enter(combo: str) -> None:
    assert keys.sends_enter(combo) is True


@pytest.mark.parametrize("combo", [
    "ctrl+s", "m", "shift+m", "alt+m", "ctrl+alt+s", "ctrl+i", "escape", "garbage", "", "ctrl+%",
])
def test_sends_enter_is_false_for_everything_else(combo: str) -> None:
    assert keys.sends_enter(combo) is False


# ----------------------------------------------------------- clipboard

def test_snapshot_keeps_every_copyable_format_and_skips_gdi_handles() -> None:
    board = FakeBoard([(CF_UNICODE, b"t"), (CF_DIB, b"dib"), (CF_BITMAP, b"gdi"), (PRIVATE_FORMAT, b"p")])
    assert keys.snapshot_clipboard(board) == [(CF_UNICODE, b"t"), (CF_DIB, b"dib"), (PRIVATE_FORMAT, b"p")]
    assert board.opened is False


def test_snapshot_skips_gdi_object_range_formats() -> None:
    board = FakeBoard([(0x0300, b"x"), (PRIVATE_FORMAT, b"p")])
    assert keys.snapshot_clipboard(board) == [(PRIVATE_FORMAT, b"p")]


def test_snapshot_fails_closed_when_the_clipboard_cannot_be_opened() -> None:
    assert keys.snapshot_clipboard(FakeBoard([(CF_DIB, b"x")], can_open=False)) is None


def test_snapshot_fails_closed_over_the_size_cap_and_closes_the_board() -> None:
    board = FakeBoard([(CF_DIB, b"x" * 11)])
    assert keys.snapshot_clipboard(board, cap=10) is None
    assert board.opened is False


def test_snapshot_fails_closed_when_a_format_cannot_be_read() -> None:
    board = FakeBoard([(CF_DIB, b"x"), (PRIVATE_FORMAT, b"y")], unavailable=(PRIVATE_FORMAT,))
    assert keys.snapshot_clipboard(board) is None


def test_write_clipboard_with_no_items_leaves_it_empty() -> None:
    board = FakeBoard([(CF_DIB, b"x")])
    assert keys.write_clipboard(board, []) is True
    assert board.items == []


def test_read_text_is_empty_without_a_text_format_and_none_when_busy() -> None:
    assert keys.read_text(FakeBoard([(PRIVATE_FORMAT, b"p")])) == ""
    assert keys.read_text(FakeBoard([], can_open=False)) is None


def test_type_text_pastes_and_then_restores_the_whole_clipboard(env) -> None:
    result = keys.type_text("Привет мир", 7)
    assert result["ok"] is True
    assert (CF_UNICODE, _utf16("Привет мир")) in env.board.writes
    assert env.board.items == [(CF_UNICODE, _utf16("old")), (PRIVATE_FORMAT, b"private")]
    assert _presses(env.auto) == ["VK_CONTROL", "VK_V"]


def test_type_text_restores_the_clipboard_even_when_the_paste_fails(env) -> None:
    env.auto.fail_on.add("VK_V")
    result = keys.type_text("secret text", 7)
    assert "error" in result
    assert "VK_CONTROL" in _releases(env.auto)
    assert env.board.items == [(CF_UNICODE, _utf16("old")), (PRIVATE_FORMAT, b"private")]


def test_type_text_pastes_nothing_when_the_clipboard_cannot_be_saved(env) -> None:
    env.board.can_open = False
    result = keys.type_text("hello", 7)
    assert result["sent"] is False
    assert env.auto.events == []


def test_clipboard_get_returns_the_text_untouched(env) -> None:
    env.board.items = [(CF_UNICODE, _utf16("hello"))]
    assert keys.clipboard_get() == {"text": "hello", "length": 5}


def test_clipboard_get_reports_a_busy_clipboard(env) -> None:
    env.board.can_open = False
    assert "error" in keys.clipboard_get()


def test_clipboard_set_replaces_the_clipboard_with_the_text(env) -> None:
    assert keys.clipboard_set("abc") == {"ok": True, "length": 3}
    assert env.board.items == [(CF_UNICODE, _utf16("abc"))]


@pytest.mark.windows
@WINDOWS_ONLY
def test_win32_clipboard_object_binds_its_api_without_touching_the_clipboard() -> None:
    assert keys._Win32Clipboard() is not None
