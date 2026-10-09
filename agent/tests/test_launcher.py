"""Launcher: refusal table, shortcut index, exact resolution and the launch path.

Each test builds its own Start Menu tree under tmp_path. os.startfile and
subprocess.Popen are replaced with recorders, so no program is ever started.
"""
from __future__ import annotations

import ctypes
import shutil
import sys
from pathlib import Path

import pytest

from coworker import launcher

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the launcher is Windows-only")


@pytest.fixture
def started(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(launcher.os, "startfile", lambda path: calls.append(path), raising=False)
    return calls


@pytest.fixture
def popened(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    return calls


@pytest.fixture
def start_menu(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "Programs"
    root.mkdir()
    monkeypatch.setattr(launcher, "_shortcut_roots", lambda: [str(root)])
    return root


def _fake_lnk(target: str = "") -> bytes:
    """Bytes shaped like a shortcut: a header, then the target in ASCII and UTF-16LE."""
    return b"L\x00\x00\x00" + b"\x00" * 60 + target.encode("ascii") + b"\x00" + target.encode("utf-16-le")


def _lnk(folder: Path, name: str, target: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.lnk"
    path.write_bytes(_fake_lnk(target))
    return path


# ------------------------------------------------------------- refusal table

@pytest.mark.parametrize("query", ["", "   ", "\u200b", "\u200b\t"])
def test_empty_query_is_refused_as_invalid(query):
    assert launcher.refusal_code(query) == "arg_invalid"


@pytest.mark.parametrize("query", [
    "cmd", "CMD.EXE", "C:\\Windows\\System32\\cmd.exe", "powershell", "Windows PowerShell",
    "pwsh", "python", "python3.13", "pythonw", "wscript", "cscript", "mshta", "rundll32",
    "regsvr32", "certutil", "bitsadmin", "msiexec", "reg", "sc", "schtasks", "wt", "wsl",
    "bash", "Command Prompt", "Командная строка", "c\u200bmd", "ＣＭＤ",
])
def test_shells_and_interpreters_are_refused(query):
    assert launcher.refusal_code(query) == "hard_deny"


@pytest.mark.parametrize("query", [
    "telegram", "Телеграм", "notepad", "calculator", "scanner", "Microsoft Edge", "Old Tool",
])
def test_ordinary_names_are_not_refused(query):
    assert launcher.refusal_code(query) is None


def test_builtin_table_never_names_a_shell():
    for display, command in launcher._SYSTEM_APPS.items():
        assert launcher.refusal_code(display) is None
        assert launcher.refusal_code(command) is None, command


# ---------------------------------------------------- shortcut target bytes

@pytest.mark.parametrize("data, refused", [
    (b"C:\\Windows\\System32\\cmd.exe", True),
    (b"%windir%\\system32\\CMD.EXE", True),
    (b"\x07" + "C:\\Windows\\System32\\cmd.exe".encode("utf-16-le"), True),       # odd offset
    ("C:\\Windows\\System32\\cmd.exe".encode("utf-16-le"), True),               # even offset
    (b"C:\\Tools\\python3.13.exe", True),
    (b"C:\\tools\\start.bat", True),
    (b"C:\\tools\\run.ps1", True),
    (b"C:\\Program Files\\Tool\\tool.exe", False),
    (b"C:\\Users\\me\\misc.exe", False),       # "sc.exe" inside a longer name
    (b"C:\\App\\notes.json", False),          # ".js" inside a longer extension
    (b"C:\\Users\\me\\AppData\\Telegram\\Telegram.exe", False),
])
def test_target_bytes_are_checked_in_ascii_and_utf16(data, refused):
    assert launcher._target_refused(data) is refused


# -------------------------------------------------------------- Start Menu index

def test_roots_are_never_relative(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("ProgramData", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("PUBLIC", raising=False)
    assert launcher._shortcut_roots() == []


def test_exact_name_resolves_to_its_shortcut(start_menu):
    path = _lnk(start_menu, "Telegram")
    assert launcher.resolve_exact("Telegram") == {"name": "Telegram", "path": str(path)}


def test_exact_match_ignores_case_and_punctuation(start_menu):
    path = _lnk(start_menu, "Old-Tool")
    assert launcher.resolve_exact("old tool") == {"name": "Old-Tool", "path": str(path)}


def test_partial_name_is_not_an_exact_match(start_menu):
    _lnk(start_menu, "Telegram")
    assert launcher.resolve_exact("Tele") is None


def test_nested_folders_are_indexed(start_menu):
    path = _lnk(start_menu / "Vendor" / "Suite", "Old Tool")
    assert launcher.resolve_exact("Old Tool")["path"] == str(path)


def test_uninstallers_are_not_indexed(start_menu):
    _lnk(start_menu, "Uninstall Foo")
    assert launcher.resolve_exact("Uninstall Foo") is None


def test_only_lnk_files_are_indexed(start_menu):
    (start_menu / "Web.url").write_text("[InternetShortcut]\nURL=https://example.com\n")
    assert launcher.resolve_exact("Web") is None


def test_shortcut_named_as_a_shell_is_left_out(start_menu):
    _lnk(start_menu, "Command Prompt", target="C:\\Windows\\System32\\cmd.exe")
    assert launcher.resolve_exact("Command Prompt") is None


def test_shortcut_with_a_harmless_name_but_a_shell_target_is_left_out(start_menu):
    _lnk(start_menu, "Console Helper", target="C:\\Windows\\System32\\cmd.exe")
    _lnk(start_menu, "Launch Helper", target="C:\\Windows\\System32\\cmd.exe")
    assert launcher.resolve_exact("Console Helper") is None
    assert launcher.resolve_exact("Launch Helper") is None


def test_shortcut_to_a_batch_script_is_left_out(start_menu):
    _lnk(start_menu, "Start Server", target="C:\\srv\\start.bat")
    assert launcher.resolve_exact("Start Server") is None


def test_ordinary_shortcut_is_indexed_next_to_a_refused_one(start_menu):
    _lnk(start_menu, "Console Helper", target="C:\\Windows\\System32\\cmd.exe")
    path = _lnk(start_menu, "Telegram", target="C:\\Users\\me\\Telegram\\Telegram.exe")
    assert launcher.resolve_exact("Telegram") == {"name": "Telegram", "path": str(path)}


@pytest.mark.windows
def test_placeholder_shortcut_is_not_read(start_menu):
    path = _lnk(start_menu, "Cloud App", target="C:\\Users\\me\\Cloud.exe")
    FILE_ATTRIBUTE_OFFLINE, FILE_ATTRIBUTE_NORMAL = 0x1000, 0x80
    ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_OFFLINE)
    try:
        assert launcher.resolve_exact("Cloud App") is None
    finally:
        ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_NORMAL)


def test_refused_query_matches_nothing(start_menu):
    _lnk(start_menu, "Telegram")
    assert launcher.find("cmd") == []
    assert launcher.find("") == []
    assert launcher.resolve_exact("cmd") is None


# ------------------------------------------------------------------ launch path

def test_launch_opens_the_exact_shortcut(start_menu, started):
    path = _lnk(start_menu, "Telegram")
    _lnk(start_menu, "Telegram Desktop")
    assert launcher.launch("Telegram") == {"ok": True, "opened": "Telegram"}
    assert started == [str(path)]


def test_ambiguous_name_returns_candidates_and_launches_nothing(start_menu, started):
    _lnk(start_menu, "Telegram Desktop")
    _lnk(start_menu, "Telemost")
    result = launcher.launch("tel")
    assert result["ambiguous"] is True
    assert sorted(result["options"]) == ["Telegram Desktop", "Telemost"]
    assert started == []


@pytest.mark.parametrize("query", ["", "   ", "cmd", "Windows PowerShell", "Командная строка"])
def test_refused_queries_launch_nothing(start_menu, started, popened, query):
    _lnk(start_menu, "Telegram", target="C:\\Users\\me\\Telegram.exe")
    result = launcher.launch(query)
    assert "error" in result and result["code"] in ("arg_invalid", "hard_deny")
    assert started == [] and popened == []


def test_unknown_name_never_consults_path(start_menu, started, popened, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("PATH lookup on a model-supplied name")

    monkeypatch.setattr(shutil, "which", forbidden)
    result = launcher.launch("zzqxv unknown program")
    assert result["code"] == "arg_invalid"
    assert started == [] and popened == []


def test_builtin_app_uses_the_fixed_command(start_menu, popened):
    assert launcher.launch("calculator") == {"ok": True, "opened": "calculator"}
    assert popened == [["calc.exe"]]


def test_failed_start_reports_an_error(start_menu, monkeypatch):
    _lnk(start_menu, "Telegram")

    def broken(path):
        raise OSError("no association")

    monkeypatch.setattr(launcher.os, "startfile", broken, raising=False)
    result = launcher.launch("Telegram")
    assert "error" in result and "no association" in result["error"]


# ------------------------------------- Linux distributions and terminals as apps

@pytest.mark.parametrize("name", [
    "Ubuntu", "Ubuntu 22.04 LTS", "Debian GNU/Linux", "kali-linux", "openSUSE-Leap-15.5",
    "Fedora", "mintty", "Git Bash", "WSL", "Windows Terminal",
])
def test_distribution_and_terminal_names_are_refused_by_name(name: str) -> None:
    assert launcher.refusal_code(name) == "hard_deny"
    assert launcher.resolve_exact(name) is None
    assert launcher.find(name) == []


@pytest.mark.parametrize("target", [
    rb"C:\Users\u\AppData\Local\Microsoft\WindowsApps\ubuntu.exe",
    rb"C:\Users\u\AppData\Local\Microsoft\WindowsApps\ubuntu2204.exe",
    rb"C:\Users\u\AppData\Local\Microsoft\WindowsApps\debian.exe",
    rb"C:\Users\u\AppData\Local\Microsoft\WindowsApps\kali.exe",
    rb"C:\Users\u\AppData\Local\Microsoft\WindowsApps\opensuse-leap-15.5.exe",
    rb"C:\Program Files\Git\git-bash.exe",
    rb"C:\Program Files\mintty\bin\mintty.exe",
    rb"C:\Windows\System32\wslhost.exe",
])
def test_a_shortcut_to_a_shell_launcher_is_not_admitted(target: bytes) -> None:
    assert launcher._target_refused(target) is True


def test_a_shell_launcher_is_found_when_its_target_is_utf16() -> None:
    assert launcher._target_refused(r"C:\tools\ubuntu.exe".encode("utf-16-le")) is True


def test_an_ordinary_application_target_is_still_admitted() -> None:
    assert launcher._target_refused(rb"C:\Program Files\Telegram\Telegram.exe") is False
    assert launcher._target_refused(rb"C:\Program Files\Mesh\mesh.exe") is False


def test_the_index_keeps_a_distribution_shortcut_out(start_menu: Path) -> None:
    _lnk(start_menu, "Notes", r"C:\Windows\notepad.exe")
    _lnk(start_menu, "Terminal", r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\ubuntu2204.exe")
    indexed = launcher._scan((str(start_menu),))
    assert "Notes" in indexed
    assert "Terminal" not in indexed
