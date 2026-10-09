"""Application and window tools: tier table, relax behaviour, handler results and argument schemas.

The Start Menu is a temporary tree and the uia window functions are fakes, so no
program starts and no real window is touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coworker import launcher, uia
from coworker.core.types import Autonomy, CallContext, Decision, Provenance, Tier
from coworker.system import WindowVisualState
from coworker.tools import apps
from coworker.tools.registry import Registry, Services, ToolCall, validate_args

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the launcher is Windows-only")

# Section 3 of docs/architecture-v2.md, the apps rows.
SECTION_3_TIERS = {
    "open_app": Tier.SYSTEM_CHANGE,
    "open_folder_in_app": Tier.SYSTEM_CHANGE,
    "window_focus": Tier.READ,
    "window_state": Tier.READ,
    "window_close": Tier.DESTRUCTIVE,
}


@pytest.fixture(autouse=True)
def _no_real_windows(monkeypatch):
    # Any real window call from these tests is a bug; each test that needs one patches it.
    for fn in ("focus_window", "set_window_state", "close_window"):
        monkeypatch.setattr(uia, fn, _forbidden(fn))


def _forbidden(name):
    def boom(*args, **kwargs):
        raise AssertionError(f"real uia.{name} called from a test")
    return boom


@pytest.fixture
def start_menu(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "Programs"
    root.mkdir()
    monkeypatch.setattr(launcher, "_shortcut_roots", lambda: [str(root)])
    return root


@pytest.fixture
def started(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(launcher.os, "startfile", lambda path: calls.append(path), raising=False)
    return calls


def _lnk(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.lnk"
    path.write_bytes(b"L\x00\x00\x00" + b"\x00" * 60 + b"C:\\Users\\me\\App.exe")
    return path


def _spec(name: str):
    return next(s for s in apps.SPECS if s.name == name)


def _ctx(provenance: Provenance = Provenance.OWNER) -> CallContext:
    return CallContext(
        turn_id="t1", actor="owner", chat_id=1, autonomy=Autonomy.ASK_FOR_WRITES,
        grants=frozenset({"apps"}), generation=0, provenance=provenance,
    )


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(name=name, args=args, ctx=_ctx(), svc=Services())


# ----------------------------------------------------------------- registry

def test_specs_register_and_carry_the_section_3_tiers():
    reg = Registry()
    reg.register_many(apps.SPECS)
    assert {s.name for s in reg.all()} == set(SECTION_3_TIERS)
    for name, tier in SECTION_3_TIERS.items():
        spec = reg.get(name)
        assert spec.family == "apps"
        assert spec.tier == tier


def test_window_close_is_irreversible_and_never_relaxed():
    spec = _spec("window_close")
    assert spec.reversible is False
    assert spec.relax is None


@pytest.mark.parametrize("name", ["window_focus", "window_state"])
def test_window_reads_have_no_relax_hook(name):
    assert _spec(name).relax is None


# -------------------------------------------------------------------- relax

def test_open_app_relaxes_only_for_an_exact_indexed_shortcut(start_menu):
    _lnk(start_menu, "Telegram")
    relax = _spec("open_app").relax
    assert relax({"name": "Telegram"}, _ctx()) is True
    assert relax({"name": "telegram"}, _ctx()) is True
    assert relax({"name": "Tele"}, _ctx()) is False
    assert relax({"name": "Telegram Web"}, _ctx()) is False
    assert relax({"name": "cmd"}, _ctx()) is False
    assert relax({"name": ""}, _ctx()) is False


def test_open_app_does_not_relax_a_builtin_app_outside_the_index(start_menu):
    assert _spec("open_app").relax({"name": "notepad"}, _ctx()) is False


def test_open_app_does_not_relax_a_browser_category(start_menu):
    _lnk(start_menu, "Google Chrome")
    assert _spec("open_app").relax({"name": "browser"}, _ctx()) is False


def test_open_app_summary_names_the_resolved_program(start_menu):
    _lnk(start_menu, "Telegram")
    assert _spec("open_app").summary({"name": "telegram"}) == "Dasturni ochish: Telegram"


def test_open_app_summary_never_shows_an_unresolved_name(start_menu):
    text = _spec("open_app").summary({"name": "zzqxv"})
    assert "zzqxv" not in text


# ----------------------------------------------------- argument check, before the card

def _check(name: str):
    return _spec("open_app").arg_checks[0]({"name": name}, _ctx(), None)


def test_arg_check_lets_an_exact_name_through(start_menu):
    _lnk(start_menu, "Telegram")
    assert _check("Telegram") is None


def test_arg_check_asks_for_a_choice_and_lists_the_options(start_menu):
    _lnk(start_menu, "Telegram Desktop")
    _lnk(start_menu, "Telemost")
    verdict = _check("tel")
    assert verdict.decision == Decision.DENY
    assert verdict.code == "need_choice"
    assert "Telegram Desktop" in verdict.reason and "Telemost" in verdict.reason


def test_arg_check_refuses_an_unknown_name_as_not_found(start_menu):
    verdict = _check("zzqxv")
    assert verdict.decision == Decision.DENY and verdict.code == "not_found"


def test_arg_check_refuses_a_category_name_as_not_found(start_menu):
    _lnk(start_menu, "Firefox Browser")
    verdict = _check("browser")
    assert verdict.decision == Decision.DENY and verdict.code == "not_found"


@pytest.mark.parametrize("name, code", [("cmd", "hard_deny"), ("  ", "arg_invalid")])
def test_arg_check_passes_the_refusal_code_through(start_menu, name, code):
    verdict = _check(name)
    assert verdict.decision == Decision.DENY and verdict.code == code


def test_open_app_carries_the_argument_check():
    assert len(_spec("open_app").arg_checks) == 1


# ------------------------------------------------------------------ open_app

def test_open_app_starts_an_exact_shortcut(start_menu, started):
    path = _lnk(start_menu, "Telegram")
    result = _spec("open_app").handler(_call("open_app", {"name": "Telegram"}))
    assert result.ok is True
    assert result.data == {"opened": "Telegram"}
    assert started == [str(path)]


def test_open_app_ambiguous_name_lists_options_and_starts_nothing(start_menu, started):
    _lnk(start_menu, "Telegram Desktop")
    _lnk(start_menu, "Telemost")
    result = _spec("open_app").handler(_call("open_app", {"name": "tel"}))
    assert result.ok is False
    assert result.code == "need_choice"
    assert sorted(result.data["options"]) == ["Telegram Desktop", "Telemost"]
    assert started == []


def test_open_app_refuses_a_shell_and_starts_nothing(start_menu, started):
    result = _spec("open_app").handler(_call("open_app", {"name": "powershell"}))
    assert result.ok is False
    assert result.code == "hard_deny"
    assert started == []


def test_open_app_empty_name_is_an_invalid_argument(start_menu, started):
    result = _spec("open_app").handler(_call("open_app", {"name": "  "}))
    assert result.ok is False
    assert result.code == "arg_invalid"
    assert started == []


def test_open_app_unknown_name_says_so(start_menu, started):
    result = _spec("open_app").handler(_call("open_app", {"name": "zzqxv"}))
    assert result.ok is False
    assert result.code == "not_found"
    assert "topilmadi" in result.error
    assert started == []


def test_open_app_never_starts_a_browser_category(start_menu, started):
    _lnk(start_menu, "Office Writer")
    result = _spec("open_app").handler(_call("open_app", {"name": "browser"}))
    assert result.ok is False and result.code == "not_found"
    assert started == []


@pytest.mark.parametrize("args, fragment", [
    ({"name": "x" * 121}, "too long"),
    ({}, "missing argument: name"),
    ({"name": "a", "path": "C:\\"}, "unexpected argument: path"),
    ({"name": 5}, "must be a string"),
])
def test_open_app_schema_rejects_bad_arguments(args, fragment):
    err = validate_args(_spec("open_app").parameters, args)
    assert err is not None and fragment in err


# ------------------------------------------------------------- window tools

def test_window_focus_reports_the_window(monkeypatch):
    seen: list[int] = []

    def fake(handle):
        seen.append(handle)
        return {"ok": True, "window": "Notepad", "foreground": True}

    monkeypatch.setattr(uia, "focus_window", fake)
    result = _spec("window_focus").handler(_call("window_focus", {"handle": 42}))
    assert result.ok is True
    assert result.data == {"window": "Notepad", "foreground": True}
    assert seen == [42]


def test_window_focus_passes_the_uia_error_back(monkeypatch):
    monkeypatch.setattr(uia, "focus_window", lambda handle: {"ok": False, "error": "Oyna topilmadi."})
    result = _spec("window_focus").handler(_call("window_focus", {"handle": 42}))
    assert result.ok is False
    assert result.error == "Oyna topilmadi."


@pytest.mark.parametrize("word, state", [
    ("minimize", WindowVisualState.MINIMIZED),
    ("maximize", WindowVisualState.MAXIMIZED),
    ("normal", WindowVisualState.NORMAL),
])
def test_window_state_maps_each_word_to_its_visual_state(monkeypatch, word, state):
    seen: list[tuple[int, int]] = []

    def fake(handle, visual):
        seen.append((handle, visual))
        return {"ok": True, "window": "Editor", "state": visual}

    monkeypatch.setattr(uia, "set_window_state", fake)
    result = _spec("window_state").handler(_call("window_state", {"handle": 7, "state": word}))
    assert result.ok is True
    assert seen == [(7, state)]
    assert result.data["state"] == word


def test_window_close_reports_a_requested_close_not_a_confirmed_one(monkeypatch):
    seen: list[int] = []

    def fake(handle):
        seen.append(handle)
        return {"ok": True, "closed": "Editor"}

    monkeypatch.setattr(uia, "close_window", fake)
    result = _spec("window_close").handler(_call("window_close", {"handle": 9}))
    assert result.ok is True
    assert result.data == {"close_requested": True, "window": "Editor"}
    assert seen == [9]


def test_window_close_failure_is_reported(monkeypatch):
    monkeypatch.setattr(uia, "close_window", lambda handle: {"error": "Oynani yopib bo'lmadi"})
    result = _spec("window_close").handler(_call("window_close", {"handle": 9}))
    assert result.ok is False


def test_window_close_summary_warns_about_unsaved_work():
    text = _spec("window_close").summary({"handle": 9})
    assert "9" in text and "Saqlanmagan" in text


@pytest.mark.parametrize("name, args, fragment", [
    ("window_focus", {"handle": 0}, "at least 1"),
    ("window_focus", {"handle": "12"}, "must be an integer"),
    ("window_focus", {"handle": True}, "must be an integer"),
    ("window_close", {}, "missing argument: handle"),
    ("window_state", {"handle": 3, "state": "hide"}, "must be one of minimize, maximize, normal"),
    ("window_state", {"handle": 3}, "missing argument: state"),
])
def test_window_schemas_reject_bad_arguments(name, args, fragment):
    err = validate_args(_spec(name).parameters, args)
    assert err is not None and fragment in err
