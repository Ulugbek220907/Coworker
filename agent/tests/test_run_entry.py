"""Argument parsing, the --doctor checklist and the entry point's exit codes.

Every probe is replaced or fed a fake. Nothing writes to the keyring, opens a
network connection or creates a window. The conftest fixtures keep COWORKER_HOME
and the keyring in a temporary folder.
"""
from __future__ import annotations

from pathlib import Path

import keyring
import pytest

import run as entry
from coworker.config import Config

DISPLAY = {
    "check_python": "Python",
    "check_keyring": "Kalit saqlagich (keyring)",
    "check_psutil": "Tizim yuki (psutil)",
    "check_telegram_token": "Telegram tokeni",
    "check_llm": "AI modeli",
    "check_home": "COWORKER_HOME",
    "check_index_path": "Fayl indeksi (baza)",
    "check_elevated": "Administrator huquqi",
}
PROBES = tuple(DISPLAY)
OPTIONAL = {"check_index_path", "check_elevated"}
SECRET_TOKEN = "123456789:" + "S" * 35


def _fake(name: str, ok: bool = True, required: bool = True, detail: str = "fake"):
    return lambda *args, **kwargs: entry.Check(name, ok, required, detail)


def _all_passing(monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
    """Replace every probe with a passing fake, except the ones given as name=probe."""
    for name in PROBES:
        if name in overrides:
            monkeypatch.setattr(entry, name, overrides[name])
        else:
            monkeypatch.setattr(entry, name, _fake(DISPLAY[name], required=name not in OPTIONAL))


def _cfg() -> Config:
    return Config()


# ------------------------------------------------------------ arguments

def test_no_flags_means_the_window_mode():
    args = entry.parse_args([])
    assert args.headless is False
    assert args.doctor is False
    assert args.config is None


def test_headless_and_doctor_flags_parse():
    assert entry.parse_args(["--headless"]).headless is True
    assert entry.parse_args(["--doctor"]).doctor is True


def test_config_path_is_a_path():
    assert entry.parse_args(["--config", "alt.json"]).config == Path("alt.json")


def test_headless_and_doctor_cannot_be_combined(capsys):
    with pytest.raises(SystemExit) as exc:
        entry.parse_args(["--headless", "--doctor"])
    assert exc.value.code == 2


# ------------------------------------------------------------ doctor checklist

def test_doctor_exits_zero_when_every_required_item_is_present(monkeypatch, capsys):
    _all_passing(monkeypatch, check_index_path=_fake("Fayl indeksi (baza)", ok=False, required=False))
    assert entry.run_doctor(_cfg()) == 0
    out = capsys.readouterr().out
    assert "Hammasi tayyor." in out
    assert "eslatma" in out  # the optional item is marked, not counted


def test_doctor_exits_one_and_names_a_missing_required_item(monkeypatch, capsys):
    _all_passing(monkeypatch, check_keyring=_fake("Kalit saqlagich (keyring)", ok=False))
    assert entry.run_doctor(_cfg()) == 1
    out = capsys.readouterr().out
    assert "Yetishmayapti" in out
    assert "Kalit saqlagich (keyring)" in out


def test_an_optional_item_never_changes_the_exit_code(monkeypatch):
    _all_passing(monkeypatch, check_elevated=_fake("Administrator huquqi", ok=False, required=False))
    assert entry.run_doctor(_cfg()) == 0


def test_every_checklist_line_is_printed(monkeypatch, capsys):
    _all_passing(monkeypatch)
    entry.run_doctor(_cfg())
    out = capsys.readouterr().out
    for name in ("Python", "Kalit saqlagich", "Tizim yuki", "Telegram tokeni", "AI modeli",
                 "COWORKER_HOME", "Fayl indeksi", "Administrator huquqi"):
        assert name in out


def test_doctor_never_prints_the_token_value(monkeypatch, capsys):
    monkeypatch.setenv("COWORKER_TELEGRAM_BOT_TOKEN", SECRET_TOKEN)
    _all_passing(monkeypatch, check_telegram_token=entry.check_telegram_token)
    assert entry.run_doctor(_cfg()) == 0
    out = capsys.readouterr().out
    assert SECRET_TOKEN not in out
    assert "saqlangan" in out


def test_missing_token_is_reported_as_missing(monkeypatch):
    monkeypatch.delenv("COWORKER_TELEGRAM_BOT_TOKEN", raising=False)
    check = entry.check_telegram_token()
    assert check.ok is False
    assert check.required is True


def test_llm_check_names_dialect_and_model_without_the_key(monkeypatch):
    cfg = _cfg()
    cfg.set("llm_base_url", "https://api.deepseek.com")
    cfg.set("llm_model", "deepseek-chat")
    monkeypatch.setenv("COWORKER_LLM_API_KEY", "sk-do-not-print-this")
    check = entry.check_llm(cfg)
    assert check.ok is True
    assert "openai" in check.detail
    assert "deepseek-chat" in check.detail
    assert "sk-do-not-print-this" not in check.detail


def test_llm_check_fails_without_a_key(monkeypatch):
    cfg = _cfg()
    cfg.set("llm_base_url", "https://api.deepseek.com")
    cfg.set("llm_model", "deepseek-chat")
    monkeypatch.delenv("COWORKER_LLM_API_KEY", raising=False)
    check = entry.check_llm(cfg)
    assert check.ok is False
    assert "kaliti yo'q" in check.detail


def test_keyring_check_fails_on_the_fail_backend(monkeypatch):
    fail_backend = type("Keyring", (), {"__module__": "keyring.backends.fail"})()
    monkeypatch.setattr(keyring, "get_keyring", lambda: fail_backend)
    assert entry.check_keyring().ok is False


def test_keyring_check_passes_on_a_real_backend(monkeypatch):
    windows_backend = type("WinVaultKeyring", (), {"__module__": "keyring.backends.Windows"})()
    monkeypatch.setattr(keyring, "get_keyring", lambda: windows_backend)
    check = entry.check_keyring()
    assert check.ok is True
    assert "WinVaultKeyring" in check.detail


def test_python_check_compares_against_the_minimum():
    check = entry.check_python()
    assert check.ok is True  # the test suite itself needs a supported Python
    assert "3.11" in check.detail


def test_home_check_shows_where_config_lives(monkeypatch, isolated_home):
    check = entry.check_home()
    assert check.ok is True
    assert str(isolated_home) in check.detail


# ------------------------------------------------------------ dispatch

def test_main_chooses_the_mode_from_the_flags(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(entry, "run_doctor", lambda cfg: calls.append("doctor") or 7)
    monkeypatch.setattr(entry, "run_headless", lambda cfg: calls.append("headless") or 8)
    monkeypatch.setattr(entry, "run_gui", lambda cfg: calls.append("gui") or 9)

    assert entry.main(["--doctor"]) == 7
    assert entry.main(["--headless"]) == 8
    assert entry.main([]) == 9
    assert calls == ["doctor", "headless", "gui"]


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("return", 0),
        ("keyboard", 0),
        ("crash", 1),
        ("exit2", 2),
    ],
)
def test_run_always_ends_the_process_with_the_right_code(monkeypatch, outcome, expected):
    seen: list[int] = []

    def fake_main(argv=None):
        if outcome == "return":
            return 0
        if outcome == "keyboard":
            raise KeyboardInterrupt
        if outcome == "crash":
            raise RuntimeError("boom")
        raise SystemExit(2)

    monkeypatch.setattr(entry, "main", fake_main)
    monkeypatch.setattr(entry, "hard_exit", lambda code=0: seen.append(code))
    entry.run()
    assert seen == [expected]


# ------------------------------------------------------------ headless console

def test_headless_status_prints_a_fresh_code_on_online(capsys):
    class FakePairing:
        def issue_code(self):
            return "12345678"

    class FakeRuntime:
        pairing = FakePairing()

    box = {"runtime": FakeRuntime()}
    status = entry.console_status(box)
    status("online", "ulash kodi: 00000000")  # the runtime's own, already stale, code
    out = capsys.readouterr().out
    assert "/connect 12345678" in out
    assert "00000000" not in out


def test_headless_status_prints_other_states_as_they_are(capsys):
    status = entry.console_status({})
    status("offline", "Telegram tokeni kiritilmagan")
    assert "[offline] Telegram tokeni kiritilmagan" in capsys.readouterr().out
