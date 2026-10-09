"""Path rules: traversal, device names, reparse points, protected roots and blocked names.

Every test runs with the profile, AppData and Coworker home redirected into a
temporary folder, so no test reads or writes the real user's files. Junction and
symbolic-link tests are skipped with a reason where the OS refuses to create them.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
from pathlib import Path

import pytest

from coworker.policy.paths import check_path

pytestmark = pytest.mark.skipif(os.name != "nt", reason="path rules are Windows rules")


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("COWORKER_HOME", str(tmp_path / "override"))
    return home


def _short_name(path: Path) -> str:
    """The 8.3 alias of an existing path, or "" when the volume has none."""
    buffer = ctypes.create_unicode_buffer(1024)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024)
    return buffer.value if 0 < length < 1024 else ""


@pytest.fixture
def junction():
    """A factory that makes a junction; each one is removed with os.rmdir, never recursively."""
    made: list[Path] = []

    def make(link: Path, target: Path) -> Path:
        target.mkdir(parents=True, exist_ok=True)
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, check=False,
        )
        if done.returncode != 0 or not link.exists():
            pytest.skip("this account cannot create junctions")
        made.append(link)
        return link

    yield make
    for link in made:
        os.rmdir(link)


# ------------------------------------------------------------------ syntax


def test_ordinary_paths_are_allowed_for_read_and_write(tmp_path):
    target = tmp_path / "notes.txt"
    assert check_path(str(target), write=False) is None
    assert check_path(str(target), write=True) is None


@pytest.mark.parametrize("value", [
    r"C:\a\..\Windows",
    "C:/a/../b",
    r"..\secret.txt",
    r"C:\a\.. \b",        # Win32 strips the trailing space, leaving ".."
    r"C:\a\...\b",        # a dot-only name with two or more dots
    r"C:\a\.\..\..\x",
])
def test_traversal_segments_are_refused_before_normalisation(value):
    assert check_path(value, write=False) == "path_invalid"


def test_dots_inside_a_name_are_not_traversal(tmp_path):
    assert check_path(str(tmp_path / "file..txt"), write=False) is None


@pytest.mark.parametrize("value", ["", "   ", "C:\\a\x00b", "C:\\a\nb", None, 42])
def test_empty_control_and_non_text_values_are_invalid(value):
    assert check_path(value, write=False) == "path_invalid"


@pytest.mark.parametrize("value", [
    "\\\\?\\C:\\x",
    "\\\\.\\C:\\x",
    "//?/C:/x",
    "\\??\\C:\\x",
    "\\\\.\\NUL",
])
def test_extended_and_device_namespace_prefixes_are_refused(value):
    assert check_path(value, write=False) == "path_invalid"


@pytest.mark.parametrize("value", [r"C:\x\notes.txt:hidden", r"C:\x:stream"])
def test_alternate_data_stream_colons_are_refused(value):
    assert check_path(value, write=False) == "path_invalid"


# ------------------------------------------------------------ device names


@pytest.mark.parametrize("value", [
    r"C:\x\CON",
    r"C:\x\nul",
    r"C:\x\NUL.txt",
    r"C:\x\com1",
    r"C:\x\COM9.log",
    r"C:\x\lpt5.tar.gz",
    r"C:\x\PRN",
    r"C:\x\aux ",
    "C:\\x\\COM\u00b9.txt",  # superscript one is COM1 to Windows
    r"C:\NUL\inside",        # a device name used as a folder
])
def test_device_names_are_refused_with_or_without_extension(value):
    assert check_path(value, write=False) == "device_name"


@pytest.mark.parametrize("name", ["COM10.txt", "console.txt", "nullify.txt", "lpt0.txt", "auxiliary.docx"])
def test_names_that_only_contain_a_device_name_are_allowed(tmp_path, name):
    assert check_path(str(tmp_path / name), write=True) is None


# --------------------------------------------------------- reparse points


def test_junction_component_is_refused_for_read_and_write(tmp_path, junction):
    link = junction(tmp_path / "link", tmp_path / "target")
    assert check_path(str(link / "file.txt"), write=False) == "reparse_point"
    assert check_path(str(link / "new" / "file.txt"), write=True) == "reparse_point"


def test_junction_itself_is_refused(tmp_path, junction):
    link = junction(tmp_path / "link", tmp_path / "target")
    assert check_path(str(link), write=False) == "reparse_point"


def test_real_folder_next_to_a_junction_is_still_allowed(tmp_path, junction):
    junction(tmp_path / "link", tmp_path / "target")
    assert check_path(str(tmp_path / "target" / "file.txt"), write=False) is None


def test_junction_into_a_protected_folder_reports_the_link_not_the_target(tmp_path, junction):
    protected = Path(os.environ["COWORKER_HOME"])
    link = junction(tmp_path / "sneaky", protected)
    assert check_path(str(link / "config.json"), write=False) == "reparse_point"


def test_symbolic_link_component_is_refused(tmp_path):
    target = tmp_path / "real.txt"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symbolic links need Developer Mode or a privilege this account lacks")
    assert check_path(str(link), write=False) == "reparse_point"


# ------------------------------------------------------- protected roots


def test_coworker_home_override_is_protected_for_read_and_write():
    inside = Path(os.environ["COWORKER_HOME"]) / "config.json"
    assert check_path(str(inside), write=False) == "protected_path"
    assert check_path(str(inside), write=True) == "protected_path"


def test_default_config_folder_is_protected(isolated_env):
    inside = isolated_env / "AppData" / "Roaming" / "Coworker" / "store" / "db.sqlite3"
    assert check_path(str(inside), write=False) == "protected_path"


def test_config_folder_name_match_is_on_a_separator_boundary(isolated_env):
    lookalike = isolated_env / "AppData" / "Roaming" / "CoworkerOld" / "notes.txt"
    assert check_path(str(lookalike), write=False) is None


@pytest.mark.parametrize("relative", [
    ".ssh/known_hosts",
    ".aws/config",
    ".gnupg/pubring.kbx",
    "AppData/Roaming/Microsoft/Protect/S-1-5-21/masterkey",
    "AppData/Roaming/Microsoft/Credentials/blob",
    "AppData/Local/Microsoft/Credentials/blob",
])
def test_user_secret_folders_are_protected(isolated_env, relative):
    assert check_path(str(isolated_env / relative), write=False) == "protected_path"


@pytest.mark.parametrize("relative", [
    "AppData/Roaming/Microsoft/Office/recent.txt",
    "AppData/Roaming/Microsoft",
    "Documents/report.docx",
])
def test_neighbours_of_protected_folders_are_not_protected(isolated_env, relative):
    assert check_path(str(isolated_env / relative), write=False) is None


def test_windows_and_program_files_are_protected_for_writes_only(tmp_path, monkeypatch):
    windows = tmp_path / "Windows"
    programs = tmp_path / "Program Files"
    monkeypatch.setenv("SystemRoot", str(windows))
    monkeypatch.setenv("ProgramFiles", str(programs))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "Program Files (x86)"))
    assert check_path(str(windows / "win.ini"), write=True) == "protected_path"
    assert check_path(str(programs / "app" / "tool.exe"), write=True) == "protected_path"
    assert check_path(str(windows / "win.ini"), write=False) is None
    assert check_path(str(programs / "app" / "tool.exe"), write=False) is None


# ----------------------------------------------------------- blocked names


@pytest.mark.parametrize("name", [
    "passwords.txt", "Mon_PAROL.docx", "api key.txt", "my_api_key.json", "SECRET-plan.txt",
    "crypto wallet.dat", "seed phrase.txt", "private key notes.txt", "id_rsa", "backup.kdbx",
    "app.keystore", "old.ppk", "server.pem", ".env", "config.env.local", "credentials.csv",
    "tokens.json",
])
def test_blocked_file_names_are_refused_case_insensitively(tmp_path, name):
    assert check_path(str(tmp_path / name), write=False) == "protected_path"
    assert check_path(str(tmp_path / name), write=True) == "protected_path"


@pytest.mark.parametrize("name", ["report.docx", "wallpaper.jpg", "environment.txt", "keys-notes.txt"])
def test_names_that_are_not_blocked_pass(tmp_path, name):
    assert check_path(str(tmp_path / name), write=False) is None


def test_blocked_words_in_a_folder_name_do_not_block_the_file(tmp_path):
    assert check_path(str(tmp_path / "passwords" / "notes.txt"), write=False) is None


# -------------------------------------------------------------- short names


def test_blocked_name_is_refused_through_its_8dot3_alias(tmp_path):
    real = tmp_path / "passwordlist_backup.txt"
    real.write_text("x", encoding="utf-8")
    alias = _short_name(real)
    if not alias or alias == str(real):
        pytest.skip("8.3 short names are disabled on this volume")
    assert check_path(alias, write=False) == "protected_path"


def test_protected_folder_is_refused_through_its_8dot3_alias(tmp_path):
    protected = Path(os.environ["COWORKER_HOME"])
    protected.mkdir(parents=True, exist_ok=True)
    (protected / "config.json").write_text("{}", encoding="utf-8")
    alias = _short_name(protected)
    if not alias or alias == str(protected):
        pytest.skip("8.3 short names are disabled on this volume")
    assert check_path(os.path.join(alias, "config.json"), write=False) == "protected_path"


# ------------------------------------------------------ UNC paths: refused before any OS call


@pytest.fixture
def os_calls(monkeypatch):
    """Record every stat, lstat, realpath and exists call; each one fails with OSError.

    The list must stay empty for a UNC path. Recording instead of raising keeps
    pytest's own file access working while the test runs.
    """
    calls: list = []

    def record(*args, **kwargs):
        calls.append(args)
        raise OSError("the OS was asked about a path")

    monkeypatch.setattr(os, "lstat", record)
    monkeypatch.setattr(os, "stat", record)
    monkeypatch.setattr(os.path, "realpath", record)
    monkeypatch.setattr(os.path, "exists", record)
    return calls


@pytest.mark.parametrize("value", [
    r"\\203.0.113.5\share\a.txt",
    "//203.0.113.5/share/a.txt",
    r"\\server\share",
    r"\\localhost\C$\Users\me\AppData\Roaming\Coworker\config.json",
    r"\\127.0.0.1\C$\Windows\win.ini",
    r"\\localhost\C$\Users\me\.ssh\id_ed25519",
])
@pytest.mark.parametrize("write", [False, True])
def test_unc_paths_are_refused_for_reads_and_writes_without_touching_the_network(value, write, os_calls):
    assert check_path(value, write=write) == "path_invalid"
    assert os_calls == []


def test_the_administrative_share_form_of_the_store_is_refused(isolated_env):
    store = Path(os.environ["APPDATA"]) / "Coworker" / "config.json"
    drive = os.path.splitdrive(str(store))[1]  # \Users\...
    assert check_path(r"\\localhost\C$" + drive, write=False) == "path_invalid"


def test_a_local_path_that_only_looks_like_a_share_is_not_refused(tmp_path):
    assert check_path(str(tmp_path / "server" / "share.txt"), write=False) is None


# ---------------------------------------------- browser credential stores and the Windows Vault


@pytest.mark.parametrize("relative", [
    "AppData/Roaming/Mozilla/Firefox/Profiles/abc.default/logins.json",
    "AppData/Roaming/Mozilla/Firefox/Profiles/abc.default/key4.db",
    "AppData/Roaming/Mozilla/Firefox/Profiles/abc.default/cookies.sqlite",
    "AppData/Local/Google/Chrome/User Data/Default/Login Data",
    "AppData/Local/Google/Chrome/User Data/Default/Cookies",
    "AppData/Local/Microsoft/Edge/User Data/Default/Login Data",
    "AppData/Local/BraveSoftware/Brave-Browser/User Data/Default/Web Data",
    "AppData/Local/Microsoft/Vault/4BF4C442-9B8A-41A0-B380-DD4A704DDB28",
    "AppData/Roaming/Microsoft/Vault/4BF4C442-9B8A-41A0-B380-DD4A704DDB28",
])
@pytest.mark.parametrize("write", [False, True])
def test_browser_profiles_and_the_vault_are_protected(isolated_env, relative, write):
    assert check_path(str(isolated_env / relative), write=write) == "protected_path"


def test_a_browser_profile_folder_as_a_search_root_is_refused(isolated_env):
    assert check_path(str(isolated_env / "AppData" / "Roaming" / "Mozilla"), write=False) == "protected_path"


@pytest.mark.parametrize("name", ["logins.json", "key4.db", "Login Data", "my login data.csv"])
def test_credential_store_file_names_are_blocked_wherever_they_are(tmp_path, name):
    assert check_path(str(tmp_path / "exports" / name), write=False) == "protected_path"


@pytest.mark.parametrize("relative", [
    "AppData/Local/Google",
    "AppData/Local/Microsoft/Edge",
    "Documents/Browser notes.docx",
])
def test_browser_neighbours_that_hold_no_profile_stay_readable(isolated_env, relative):
    assert check_path(str(isolated_env / relative), write=False) is None


# --------------------------------------- files the assistant generated, readable for sending


def test_a_generated_pdf_in_scratch_is_readable(tmp_path):
    scratch = Path(os.environ["COWORKER_HOME"]) / "scratch" / "contract.pdf"
    assert check_path(str(scratch), write=False) is None


def test_the_scratch_folder_and_generated_folder_are_readable(tmp_path):
    home = Path(os.environ["COWORKER_HOME"])
    assert check_path(str(home / "scratch"), write=False) is None
    assert check_path(str(home / "generated" / "shot.png"), write=False) is None


def test_scratch_stays_unwritable_through_the_tools(tmp_path):
    scratch = Path(os.environ["COWORKER_HOME"]) / "scratch" / "x.pdf"
    assert check_path(str(scratch), write=True) == "protected_path"


def test_the_config_folder_next_to_scratch_stays_protected(tmp_path):
    home = Path(os.environ["COWORKER_HOME"])
    assert check_path(str(home / "config.json"), write=False) == "protected_path"
    assert check_path(str(home / "store" / "db.sqlite3"), write=False) == "protected_path"
    assert check_path(str(home / "browser-profile" / "Default" / "Login Data"), write=False) == "protected_path"


def test_scratch_does_not_open_a_way_back_to_the_config_folder(tmp_path):
    home = Path(os.environ["COWORKER_HOME"])
    assert check_path(str(home / "scratch" / ".." / "config.json"), write=False) == "path_invalid"


def test_a_default_location_scratch_folder_is_readable(isolated_env):
    scratch = isolated_env / "AppData" / "Roaming" / "Coworker" / "scratch" / "out.pdf"
    assert check_path(str(scratch), write=False) is None
    assert check_path(str(scratch), write=True) == "protected_path"


def test_a_junction_inside_scratch_cannot_lead_into_the_store(tmp_path, junction):
    home = Path(os.environ["COWORKER_HOME"])
    store = home / "store"
    store.mkdir(parents=True, exist_ok=True)
    (home / "scratch").mkdir(parents=True, exist_ok=True)
    link = junction(home / "scratch" / "link", store)
    assert check_path(str(link / "db.sqlite3"), write=False) == "reparse_point"
