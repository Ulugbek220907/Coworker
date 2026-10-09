"""Read-only application lookups: find_app, installed_browsers and default_browser.

The registry is replaced by a fake, so the real HKCU hive is never read. The Start
Menu is a temporary tree, and os.startfile and subprocess.Popen are recorders, so no
program starts. Each test that could reach launcher.launch fails if it does.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coworker import launcher
from coworker.core.types import Autonomy, CallContext, Provenance, Tier
from coworker.tools import app_lookup
from coworker.tools.registry import Registry, Services, ToolCall, validate_args

HIVE = "HKCU"


class FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    """Stands in for winreg: one UserChoice ProgId, or no key at all."""

    HKEY_CURRENT_USER = HIVE

    def __init__(self, prog_id: str | None = None, *, missing: bool = False) -> None:
        self.prog_id = prog_id
        self.missing = missing
        self.opened: list[tuple[str, str]] = []

    def OpenKey(self, hive, path):  # noqa: N802 - winreg's own spelling
        self.opened.append((hive, path))
        if self.missing:
            raise FileNotFoundError(path)
        return FakeKey()

    def QueryValueEx(self, key, name):  # noqa: N802
        if self.prog_id is None or name != "ProgId":
            raise FileNotFoundError(name)
        return (self.prog_id, 1)


@pytest.fixture
def start_menu(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "Programs"
    root.mkdir()
    monkeypatch.setattr(launcher, "_shortcut_roots", lambda: [str(root)])
    return root


@pytest.fixture
def started(monkeypatch):
    calls: list = []
    monkeypatch.setattr(launcher.os, "startfile", lambda path: calls.append(path), raising=False)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda argv, **kw: calls.append(argv))

    def forbidden(query):
        raise AssertionError("a lookup must never launch")

    monkeypatch.setattr(launcher, "launch", forbidden)
    return calls


def _lnk(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.lnk"
    path.write_bytes(b"L\x00\x00\x00" + b"\x00" * 60 + b"C:\\Users\\me\\App.exe")
    return path


def _ctx() -> CallContext:
    return CallContext(
        turn_id="t1", actor="owner", chat_id=1, autonomy=Autonomy.ASK_FOR_WRITES,
        grants=frozenset({"apps"}), generation=0, provenance=Provenance.OWNER,
    )


def _spec(name: str):
    return next(s for s in app_lookup.SPECS if s.name == name)


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(name=name, args=args, ctx=_ctx(), svc=Services())


# ------------------------------------------------------------------ registration

def test_the_three_lookups_are_read_tools_in_the_apps_family():
    reg = Registry()
    reg.register_many(app_lookup.SPECS)
    assert {s.name for s in reg.all()} == {"find_app", "installed_browsers", "default_browser"}
    for spec in reg.all():
        assert spec.tier == Tier.READ
        assert spec.family == "apps"
        assert spec.relax is None
        assert spec.reversible is True


def test_find_app_requires_a_query_and_nothing_else():
    schema = _spec("find_app").parameters
    assert validate_args(schema, {"query": "telegram"}) is None
    assert "missing argument: query" in validate_args(schema, {})
    assert "unexpected argument: name" in validate_args(schema, {"query": "x", "name": "y"})


# ------------------------------------------------------------------- find_app

def test_find_app_reports_an_exact_name(start_menu, started):
    _lnk(start_menu, "Telegram")
    assert app_lookup.find_app("telegram") == {"status": "exact", "name": "Telegram", "options": []}


def test_find_app_offers_options_for_a_partial_name(start_menu, started):
    _lnk(start_menu, "Telegram Desktop")
    _lnk(start_menu, "Telemost")
    res = app_lookup.find_app("tel")
    assert res["status"] == "choose"
    assert res["name"] is None
    assert sorted(res["options"]) == ["Telegram Desktop", "Telemost"]


def test_find_app_says_none_for_a_category_word(start_menu, started):
    _lnk(start_menu, "Google Chrome")
    assert app_lookup.find_app("browser") == {"status": "none", "name": None, "options": []}


def test_find_app_refuses_a_shell_name(start_menu, started):
    assert app_lookup.find_app("cmd")["status"] == "refused"


def test_find_app_handler_returns_the_resolution(start_menu, started):
    _lnk(start_menu, "Telegram")
    result = _spec("find_app").handler(_call("find_app", {"query": "Telegram"}))
    assert result.ok is True
    assert result.data == {"status": "exact", "name": "Telegram", "options": []}


# ------------------------------------------------------------ installed_browsers

def test_installed_browsers_lists_the_exact_names_only(start_menu, started):
    _lnk(start_menu, "Google Chrome")
    _lnk(start_menu, "Microsoft Edge")
    _lnk(start_menu, "Firefox Developer Edition")
    assert app_lookup.installed_browsers() == {"installed": ["Google Chrome", "Microsoft Edge"]}


def test_chrome_remote_desktop_is_not_reported_as_google_chrome(start_menu, started):
    _lnk(start_menu, "Chrome Remote Desktop")
    assert app_lookup.installed_browsers() == {"installed": []}


def test_installed_browsers_is_empty_with_no_start_menu(start_menu, started):
    assert app_lookup.installed_browsers() == {"installed": []}


def test_installed_browsers_handler_returns_the_list(start_menu, started):
    _lnk(start_menu, "Brave")
    result = _spec("installed_browsers").handler(_call("installed_browsers", {}))
    assert result.ok is True and result.data == {"installed": ["Brave"]}


# ----------------------------------------------------------------- default_browser

@pytest.mark.parametrize("prog_id, name", [
    ("ChromeHTML", "Google Chrome"),
    ("MSEdgeHTM", "Microsoft Edge"),
    ("FirefoxURL-308046B0AF4A39CB", "Firefox"),
    ("BraveHTML", "Brave"),
    ("OperaStable", "Opera"),
])
def test_default_browser_maps_each_handler_family(monkeypatch, prog_id, name):
    fake = FakeWinreg(prog_id=prog_id)
    monkeypatch.setattr(app_lookup, "winreg", fake)
    assert app_lookup.default_browser() == {"name": name, "prog_id": prog_id}


def test_default_browser_reads_the_https_user_choice_of_the_current_user(monkeypatch):
    fake = FakeWinreg(prog_id="ChromeHTML")
    monkeypatch.setattr(app_lookup, "winreg", fake)
    app_lookup.default_browser()
    assert fake.opened == [(HIVE, app_lookup._USER_CHOICE)]
    assert app_lookup._USER_CHOICE.endswith(r"UrlAssociations\https\UserChoice")


def test_default_browser_keeps_an_unknown_handler_without_a_name(monkeypatch):
    monkeypatch.setattr(app_lookup, "winreg", FakeWinreg(prog_id="IE.HTTP"))
    assert app_lookup.default_browser() == {"name": None, "prog_id": "IE.HTTP"}


def test_default_browser_matches_the_handler_prefix_only(monkeypatch):
    monkeypatch.setattr(app_lookup, "winreg", FakeWinreg(prog_id="XChromeHTML"))
    assert app_lookup.default_browser() == {"name": None, "prog_id": "XChromeHTML"}


def test_default_browser_is_unknown_when_the_key_is_missing(monkeypatch):
    monkeypatch.setattr(app_lookup, "winreg", FakeWinreg(missing=True))
    assert app_lookup.default_browser() == {"name": None, "prog_id": None}


def test_default_browser_is_unknown_without_winreg(monkeypatch):
    monkeypatch.setattr(app_lookup, "winreg", None)
    assert app_lookup.default_browser() == {"name": None, "prog_id": None}


def test_default_browser_handler_returns_the_mapping(monkeypatch):
    monkeypatch.setattr(app_lookup, "winreg", FakeWinreg(prog_id="MSEdgeHTM"))
    result = _spec("default_browser").handler(_call("default_browser", {}))
    assert result.ok is True
    assert result.data == {"name": "Microsoft Edge", "prog_id": "MSEdgeHTM"}


# ------------------------------------------------------------- nothing is started

def test_no_lookup_starts_a_program(start_menu, started, monkeypatch):
    _lnk(start_menu, "Telegram")
    _lnk(start_menu, "Google Chrome")
    monkeypatch.setattr(app_lookup, "winreg", FakeWinreg(prog_id="ChromeHTML"))
    for name, args in (
        ("find_app", {"query": "tel"}),
        ("find_app", {"query": "browser"}),
        ("installed_browsers", {}),
        ("default_browser", {}),
    ):
        _spec(name).handler(_call(name, args))
    assert started == []
