"""Command rules for the shell tools: a read-only table and a hard-deny list.

Two questions, answered by two mechanisms.

``READONLY`` lists commands that may run without a tap. The model names a fixed
id and at most one argument that passes a validator. The argv is built here and
run without a shell, so an argument cannot add a second command.

``hard_deny`` lists what must never run, even after the owner taps Yes:
permanent deletion, disk and boot changes, security-setting changes, and
download-and-run forms. It matches a normalised form of the free-text command,
because cmd.exe and PowerShell both ignore carets, quotes and backticks, accept
any letter case, accept abbreviations of switches, and accept several separators.
It is a floor against mistakes and careless suggestions, not a sandbox. A command
written in a form no pattern names still reaches the owner's tap, which is why
shell_run always asks first.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from ..core.types import normalize_text
from . import paths

# Marks where the single validated argument goes in an argv template.
SLOT = "{arg}"

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# cmd.exe and PowerShell read these characters as syntax, even inside quotes
# (percent and bang expand; the rest chain or redirect). "/" is refused so no
# switch can hide in the path. A space is allowed: list2cmdline quotes the whole
# argument, so it stays one token.
_DIRECTORY = re.compile(r"[A-Za-z]:\\[^\"%!&|<>^`,;=*?/\x00-\x1f]*")


def _valid_name(arg: str) -> bool:
    return _NAME.fullmatch(arg) is not None


def _valid_directory(arg: str) -> bool:
    return _DIRECTORY.fullmatch(arg) is not None and paths.check_path(arg, write=False) is None


@dataclass(frozen=True)
class ReadonlyCommand:
    """A fixed program and flags, with at most one slot for a validated argument."""

    argv: tuple[str, ...]
    validator: Optional[Callable[[str], bool]] = None  # None when the command takes no argument

    def build(self, arg: Optional[str] = None) -> Optional[list[str]]:
        """The argv to run, or None when the argument is missing, unexpected or invalid."""
        if self.validator is None:
            return None if arg is not None else list(self.argv)
        if arg is None or not self.validator(arg):
            return None
        return [arg if part == SLOT else part for part in self.argv]


READONLY: dict[str, ReadonlyCommand] = {
    "sysinfo": ReadonlyCommand(("systeminfo",)),
    "ipconfig": ReadonlyCommand(("ipconfig", "/all")),
    "whoami": ReadonlyCommand(("whoami",)),
    "tasklist": ReadonlyCommand(("tasklist",)),
    "date": ReadonlyCommand(("powershell", "-NoProfile", "-Command", "Get-Date")),
    "where": ReadonlyCommand(("where.exe", SLOT), _valid_name),
    "dir": ReadonlyCommand(("cmd", "/c", "dir", SLOT), _valid_directory),
    "netstat": ReadonlyCommand(("netstat", "-ano", "-p", "TCP")),
}


# Characters that cmd.exe and PowerShell ignore or treat as escapes. Removing
# them first lets one pattern match every spelling of the same command.
_IGNORED = re.compile(r"[\^\"'`]")
# cmd.exe treats these as argument separators, so "del,/s" is "del /s".
_SEPARATORS = re.compile(r"[,;=]")
# PowerShell reads every one of these as the dash of a parameter, so an en dash
# before Recurse is the same switch as -Recurse. NFKC leaves most of them alone,
# which is why they are mapped here.
_DASHES = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2015": "-", "\u2212": "-", "\ufe58": "-", "\ufe63": "-", "\uff0d": "-",
})

_PATTERNS: tuple[tuple[str, str], ...] = (
    ("format", r"(?<![\w.-])format(?:\.com|\.exe)?(?=[\s/]|$)"),
    ("disk_wipe", r"\b(?:format-volume|clear-disk|initialize-disk|remove-partition)\b"),
    ("diskpart", r"\bdiskpart(?:\.exe)?\b"),
    ("bcdedit", r"\bbcdedit(?:\.exe)?\b"),
    ("reg_delete", r"\breg(?:\.exe)?\s+delete\b"),
    ("reg_add", r"\breg(?:\.exe)?\s+add\b"),
    ("shutdown", r"\bshutdown(?:\.exe)?\b|\b(?:stop|restart)-computer\b"),
    ("erase", r"\berase(?:\.com|\.exe)?\b"),
    ("delete_recursive", r"(?<![\w.-])(?:del|rd|rmdir)(?:\.com|\.exe)?(?=[\s/]).*?/s(?![a-z])"),
    ("cipher_wipe", r"\bcipher(?:\.exe)?(?=[\s/]).*?/w(?![a-z])"),
    ("sdelete", r"\bsdelete(?:64)?(?:\.exe)?\b"),
    (
        "remove_item_forced",
        r"\b(?:remove-item|ri|rm)\b.*?\s-(?:r(?:e(?:c(?:u(?:r(?:s(?:e)?)?)?)?)?)?|fo(?:r(?:c(?:e)?)?)?)(?![a-z])",
    ),
    ("clear_recyclebin", r"\bclear-recyclebin\b"),
    ("vssadmin_delete", r"\bvssadmin(?:\.exe)?\s+delete\b"),
    ("shadow_copy_delete", r"\bwmic(?:\.exe)?\b.*?\bshadowcopy\b.*?\bdelete\b"),
    ("netsh_change", r"\bnetsh(?:\.exe)?\s+(?:advfirewall\s+(?!show\b)|firewall\b)"),
    ("defender_change", r"\b(?:set|add|remove)-mppreference\b"),
    ("invoke_expression", r"\binvoke-expression\b|\biex\b"),
    ("download_string", r"\bdownloadstring\b"),
    ("download_file", r"\bdownloadfile\b"),
    ("start_process_runas", r"\bstart-process\b.*?\s-v[a-z]*[\s:]+runas\b"),
    ("runas", r"\brunas(?:\.exe)?\b"),
    ("takeown", r"\btakeown(?:\.exe)?\b"),
    ("icacls_grant", r"\bicacls(?:\.exe)?\b.*?/grant(?![a-z])"),
    ("schtasks_create", r"\bschtasks(?:\.exe)?\b.*?/create(?![a-z])"),
    ("sc_create_delete", r"\bsc(?:\.exe)?\s+(?:create|delete)\b"),
    ("wmic_process_create", r"\bwmic(?:\.exe)?\b.*?\bcall\s+create\b"),
    # One item, no Recycle Bin: del, erase, rd, rmdir and their PowerShell
    # aliases (Remove-Item, ri, rm) delete at once whatever the switches. Only a
    # word boundary before the name counts, so a file called "del.txt" does not match.
    (
        "permanent_delete",
        r"(?<![\w.\\:/-])(?:del|erase|rd|rmdir|remove-item|ri|rm|clear-content)(?:\.com|\.exe)?(?=[\s/]|$)",
    ),
    ("robocopy_purge", r"\brobocopy(?:\.exe)?\b.*?/(?:mir|purge)(?![a-z])"),
    (
        "dotnet_delete",
        r"\bio\.(?:file|directory)\]?\s*(?:::|\.)\s*delete\b"
        r"|\bfileio\.filesystem\]?\s*::\s*delete(?:file|directory)\b",
    ),
)
_COMPILED = tuple((rule, re.compile(pattern)) for rule, pattern in _PATTERNS)
_POWERSHELL = re.compile(r"\b(?:powershell|pwsh)(?:\.exe)?\b")


def normalise_command(text: str) -> str:
    """The form the hard-deny patterns are matched against.

    Dash variants become an ASCII hyphen first, because PowerShell takes each of
    them as the start of a switch, and only an ASCII hyphen matches the patterns.
    """
    text = _IGNORED.sub("", normalize_text(text).translate(_DASHES))
    return normalize_text(_SEPARATORS.sub(" ", text))


def hard_deny(text: str) -> Optional[str]:
    """The id of the first hard-deny rule the command matches, or None.

    The id goes into the audit trail, so the owner can see which rule fired.
    """
    command = normalise_command(text)
    for rule, pattern in _COMPILED:
        if pattern.search(command):
            return rule
    if _encoded_powershell(command):
        return "encoded_command"
    return None


def classify_free_text(text: str) -> Optional[str]:
    """``hard_deny`` when a free-text command matches a rule, else None."""
    return "hard_deny" if hard_deny(text) else None


def _encoded_powershell(command: str) -> bool:
    """-EncodedCommand or an unambiguous prefix of it (-e, -en, -enc ...) given to PowerShell.

    Only PowerShell gives that switch its meaning, so other programs' -e flags
    are not matched. An encoded command would bypass every text rule above.
    """
    if _POWERSHELL.search(command) is None:
        return False
    return any(_is_switch_prefix(token, "encodedcommand") for token in command.split(" "))


def _is_switch_prefix(token: str, word: str) -> bool:
    name = token.split(":", 1)[0]
    return len(name) >= 2 and name.startswith("-") and word.startswith(name[1:])
