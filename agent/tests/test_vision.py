"""Screen capture: blocked windows, no focus changes, paint-over on full-screen grabs, DPI awareness once.

Every Win32 call is replaced by a fake that returns scripted window rectangles
and pixels, so nothing here moves a window, takes a screenshot of the owner's
desktop or changes process state. Two read-only Windows checks are marked.
"""
from __future__ import annotations

import base64
import io
import os

import pytest
from PIL import Image

from coworker import uia, vision
from coworker.vision import WindowInfo

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="needs the Windows user32 API")


def _decode(image_b64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(image_b64))).convert("RGB")


def _window(handle: int, title: str, rect=(0, 0, 200, 100), minimized: bool = False, class_name: str = "") -> WindowInfo:
    return WindowInfo(handle=handle, title=title, class_name=class_name, rect=rect, minimized=minimized)


@pytest.fixture
def screen(monkeypatch):
    """Capture runs as on Windows, with DPI setup stubbed out and focus changes forbidden."""
    monkeypatch.setattr(vision, "available", lambda: True)
    monkeypatch.setattr(vision, "_dpi_done", False)
    monkeypatch.setattr(vision, "_set_dpi_awareness", lambda: None)
    for name in ("focus_window", "_force_foreground", "ensure_foreground", "set_window_state"):
        monkeypatch.setattr(uia, name, _forbidden(name))
    return monkeypatch


def _forbidden(name: str):
    def refuse(*args, **kwargs):
        raise AssertionError(f"capture must never call {name}")
    return refuse


# -------------------------------------------------------------- pure helpers

def test_fit_size_keeps_small_images_and_shrinks_large_ones() -> None:
    assert vision.fit_size(800, 600) == (800, 600)
    assert vision.fit_size(2800, 1400) == (1400, 700)
    assert vision.fit_size(1, 5000) == (1, 1400)


def test_mask_rects_selects_only_blocked_windows() -> None:
    windows = [
        _window(1, "KeePass - Vault", rect=(0, 0, 10, 10)),
        _window(2, "Notes", rect=(20, 20, 30, 30)),
        _window(3, "Login", class_name="Credential Dialog Xaml Host", rect=(40, 40, 50, 50)),
    ]
    assert vision.mask_rects(windows, uia.DEFAULT_BLOCKED_WINDOWS) == [(0, 0, 10, 10), (40, 40, 50, 50)]


# ----------------------------------------------------- one window by handle

def _must_not_grab(*args):
    raise AssertionError("this window must never be grabbed")


def test_capture_refuses_a_blocked_window_without_grabbing_it(screen) -> None:
    screen.setattr(vision, "_window_info", lambda handle: _window(5, "KeePass - Vault"))
    screen.setattr(vision, "_print_window", _must_not_grab)
    result = vision.capture(5)
    assert result["code"] == "blocked_window"
    assert "image_b64" not in result


def test_capture_refuses_a_blocked_class_name(screen) -> None:
    screen.setattr(vision, "_window_info", lambda handle: _window(6, "Sign in", class_name="BITWARDEN_UI"))
    screen.setattr(vision, "_print_window", _must_not_grab)
    assert vision.capture(6)["code"] == "blocked_window"


def test_capture_refuses_a_minimized_window_instead_of_restoring_it(screen) -> None:
    screen.setattr(vision, "_window_info", lambda handle: _window(7, "Notes", minimized=True))
    screen.setattr(vision, "_print_window", _must_not_grab)
    result = vision.capture(7)
    assert "error" in result
    assert "code" not in result


def test_capture_of_a_window_never_changes_focus_and_returns_its_pixels(screen) -> None:
    screen.setattr(vision, "_window_info", lambda handle: _window(8, "Notes", rect=(0, 0, 200, 100)))
    screen.setattr(vision, "_print_window", lambda hwnd, w, h: Image.new("RGB", (w, h), (250, 250, 250)))
    result = vision.capture(8)
    assert result["scope"] == "Notes"
    assert result["size"] == (200, 100)
    assert _decode(result["image_b64"]).getpixel((100, 50))[0] > 200


def test_capture_reports_a_failed_grab(screen) -> None:
    screen.setattr(vision, "_window_info", lambda handle: _window(9, "Notes"))
    screen.setattr(vision, "_print_window", lambda *a: None)
    assert "error" in vision.capture(9)


def test_a_win32_failure_becomes_a_refused_capture_not_a_crash(screen) -> None:
    screen.setattr(vision, "_window_info", lambda handle: _window(13, "Notes"))

    def broken(hwnd, width, height):
        raise OSError("device context unavailable")

    screen.setattr(vision, "_print_window", broken)
    result = vision.capture(13)
    assert result["error"].startswith("Skrinshot olinmadi")


def test_capture_refuses_a_window_with_no_size(screen) -> None:
    screen.setattr(vision, "_window_info", lambda handle: _window(10, "Notes", rect=(5, 5, 5, 5)))
    assert "error" in vision.capture(10)


def test_custom_blocked_patterns_from_the_owner_apply(screen) -> None:
    screen.setattr(vision, "_window_info", lambda handle: _window(11, "Notes"))
    result = vision.capture(11, blocked=("notes",))
    assert result["code"] == "blocked_window"


def test_capture_for_a_missing_handle_says_so(screen) -> None:
    screen.setattr(vision, "_window_info", lambda handle: None)
    assert vision.capture(12)["error"] == "Oyna topilmadi."


# ------------------------------------------------------------ full screen

def test_full_screen_paints_blocked_windows_over(screen) -> None:
    primary = Image.new("RGB", (200, 100), (255, 255, 255))
    screen.setattr(vision, "_grab_primary", lambda: primary)
    screen.setattr(vision, "_visible_windows", lambda: [
        _window(1, "KeePass", rect=(0, 0, 100, 100)),
        _window(2, "Notes", rect=(100, 0, 200, 100)),
    ])
    result = vision.capture(0)
    picture = _decode(result["image_b64"])
    assert picture.getpixel((50, 50))[0] < 60, "the blocked window must be painted over"
    assert picture.getpixel((150, 50))[0] > 200, "other windows stay readable"
    assert result["scope"] == "butun ekran"


def test_full_screen_without_blocked_windows_is_unchanged(screen) -> None:
    screen.setattr(vision, "_grab_primary", lambda: Image.new("RGB", (200, 100), (255, 255, 255)))
    screen.setattr(vision, "_visible_windows", lambda: [_window(2, "Notes")])
    assert _decode(vision.capture(0)["image_b64"]).getpixel((50, 50))[0] > 200


def test_full_screen_is_downscaled_to_the_model_size(screen) -> None:
    screen.setattr(vision, "_grab_primary", lambda: Image.new("RGB", (2800, 1400), (255, 255, 255)))
    screen.setattr(vision, "_visible_windows", lambda: [])
    assert vision.capture(0)["size"] == (1400, 700)


# ---------------------------------------------------- availability and DPI

def test_capture_reports_status_when_it_cannot_run(screen) -> None:
    screen.setattr(vision, "available", lambda: False)
    assert "error" in vision.capture(0)


def test_dpi_awareness_is_set_once_per_process(screen) -> None:
    calls: list[int] = []
    screen.setattr(vision, "_set_dpi_awareness", lambda: calls.append(1))
    vision.ensure_dpi_aware()
    vision.ensure_dpi_aware()
    assert calls == [1]


def test_every_capture_does_not_repeat_the_dpi_call(screen) -> None:
    calls: list[int] = []
    screen.setattr(vision, "_set_dpi_awareness", lambda: calls.append(1))
    screen.setattr(vision, "_grab_primary", lambda: Image.new("RGB", (10, 10)))
    screen.setattr(vision, "_visible_windows", lambda: [])
    vision.capture(0)
    vision.capture(0)
    assert calls == [1]


def test_prompts_are_present_and_in_owner_language() -> None:
    assert "QISQA" in vision.DEFAULT_PROMPT
    assert vision.APP_PROMPT


# ------------------------------------------------------------- Windows only

@pytest.mark.windows
@WINDOWS_ONLY
def test_a_null_handle_has_no_window_info() -> None:
    assert vision._window_info(0) is None


@pytest.mark.windows
@WINDOWS_ONLY
def test_visible_window_enumeration_is_read_only_and_well_formed() -> None:
    windows = vision._visible_windows()
    assert isinstance(windows, list)
    for info in windows:
        left, top, right, bottom = info.rect
        assert right >= left and bottom >= top
        assert info.minimized is False
