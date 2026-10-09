r"""Filesystem path rules for every tool that reads or writes a path.

The kernel calls ``check_path`` for each argument named in a tool's ``path_args``
(kernel step 5), and tools call it again before they open a file. It returns the
first refusal code from architecture section 13, or None.

The checks run in this order, and the order is part of the design:

1. Syntax: traversal segments (".."), NUL and control characters, extended and
   device-namespace prefixes, and any colon after the drive letter. A colon
   starts an alternate data stream such as ``notes.txt:hidden``, which hides data
   from listings and from the name checks. ``path_invalid``.
2. Network names: a UNC path (``\\server\share``, ``\\localhost\C$``) is refused
   before any OS call. Inspecting such a name makes Windows connect to the host
   and answer its NTLM challenge with the user's credentials, and the
   administrative shares name local files under a second spelling.
   ``path_invalid``.
3. Device names (CON, NUL, COM1-9, LPT1-9), with or without an extension. These
   are refused before the filesystem is touched. ``device_name``.
4. Reparse points: every existing component is inspected with ``os.lstat``. A
   junction or symbolic link anywhere on the path is refused, so a link inside an
   allowed folder cannot lead into a protected one. ``reparse_point``.
5. Protected roots, compared as written and after resolving 8.3 short names and
   links with ``realpath``. PROGRA~1 and Program Files name the same folder, and
   the check must see both. ``protected_path``. For writes, Windows and Program
   Files are protected as well. The config folder's ``scratch`` and ``generated``
   folders are an exception for reads only: files the assistant made there must
   be readable so that they can be sent to the owner.
6. Blocked names: the final component, as written and resolved, is matched
   against ``config.DEFAULT_BLOCKED`` and the credential-store file names. The
   resolved form matters because a short name such as PASSWO~1.TXT hides
   ``password`` from a plain match. ``protected_path``.

Relative values are made absolute against the process working directory first.
Everything here is Windows-specific; the agent runs on Windows only.
"""
from __future__ import annotations

import os
import re
import stat
from typing import Optional

from ..config import APP_NAME, DEFAULT_BLOCKED

# FILE_ATTRIBUTE_REPARSE_POINT: set on junctions, symbolic links and other reparse points.
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

_CONTROL = re.compile(r"[\x00-\x1f]")
# The \\?\, \\.\ and \??\ prefixes address the object namespace directly and
# skip the normal Win32 name rules, so no ordinary path check applies to them.
_EXTENDED_PREFIXES = ("\\\\?\\", "\\\\.\\", "\\??\\")
_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_SUPERSCRIPT_DIGITS = str.maketrans("¹²³", "123")
# File names that hold saved logins and keys, wherever they are kept. Browser
# profiles are protected as folders; these catch a copy made elsewhere.
_CREDENTIAL_FILES = ("key4.db", "logins.json", "login data")
_BLOCKED = tuple(p.strip().lower() for p in DEFAULT_BLOCKED if p.strip()) + _CREDENTIAL_FILES
# Generated files are written to these two folders inside the config folder.
_OUTPUT_FOLDERS = ("scratch", "generated")


def check_path(value: str, *, write: bool) -> Optional[str]:
    """The refusal code for a path used for reading, or for writing when ``write`` is true."""
    if not isinstance(value, str) or not value.strip() or _CONTROL.search(value):
        return "path_invalid"
    text = value.replace("/", "\\")
    if text.startswith(_EXTENDED_PREFIXES):
        return "path_invalid"
    if text.startswith("\\\\"):
        # Refused before splitdrive or abspath: a UNC name is a host, not a drive.
        return "path_invalid"
    drive, rest = os.path.splitdrive(text)
    if ":" in rest:
        return "path_invalid"
    segments = [part for part in rest.split("\\") if part]
    if any(_is_traversal(part) for part in segments):
        return "path_invalid"
    if any(_is_device(part) for part in segments):
        return "device_name"

    absolute = os.path.abspath(text)
    refusal = _reparse_refusal(absolute)
    if refusal:
        return refusal
    names = {os.path.normcase(absolute), os.path.normcase(os.path.realpath(absolute))}
    if _protected(names, write):
        return "protected_path"
    if any(_blocked(os.path.basename(name)) for name in names):
        return "protected_path"
    return None


def _is_traversal(part: str) -> bool:
    # Win32 strips trailing dots and spaces from a name, so "..", ".. " and "..."
    # all reach the parent or nothing at all. Refuse every dot-only name with two
    # or more dots, before normalisation can hide the meaning.
    return part.count(".") >= 2 and not part.strip(" .")


def _is_device(part: str) -> bool:
    stem = part.split(".", 1)[0].rstrip(" ").translate(_SUPERSCRIPT_DIGITS).upper()
    return stem in _DEVICE_NAMES


def _reparse_refusal(absolute: str) -> Optional[str]:
    """Inspect each component that exists. The first missing one ends the walk."""
    drive, rest = os.path.splitdrive(absolute)
    current = drive + os.sep
    for part in (p for p in rest.split(os.sep) if p):
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError:
            # The component exists but cannot be inspected. Refuse rather than guess.
            return "path_invalid"
        if _is_link(info):
            return "reparse_point"
    return None


def _is_link(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT) or stat.S_ISLNK(info.st_mode)


def _protected(names: set[str], write: bool) -> bool:
    """True when a name lies in a protected folder and in no folder the owner may receive from.

    The output folders sit inside the config folder, so the config folder cannot
    be a protected root for reads if they are to be sent. Writes through a tool
    never reach the output folders; the office and browser modules write them.
    """
    roots = _protected_roots(write)
    outputs = set() if write else _output_roots()
    return any(
        any(_inside(name, root) for root in roots) and not any(_inside(name, out) for out in outputs)
        for name in names
    )


def _config_bases() -> list[str]:
    """The config folder at its default place, and at the COWORKER_HOME override when one is set."""
    bases = [os.path.join(_appdata(), APP_NAME)]
    override = os.environ.get("COWORKER_HOME")
    if override:
        bases.append(override)
    return bases


def _appdata() -> str:
    return os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")


def _protected_roots(write: bool) -> set[str]:
    """Protected folders, resolved, for the current environment.

    The environment is read on every call, so a test or a moved profile takes
    effect at once. The config folder is protected at both places it can be:
    the default location and the COWORKER_HOME override, when one is set. The
    browser profile and the store live inside the config folder, so protecting
    the folder covers them.

    Browser profiles hold saved passwords, cookies and payment details, and the
    Windows Vault holds the credentials Windows keeps for the user. Both are
    protected as whole folders, for reads and writes alike.
    """
    home = os.path.expanduser("~")
    appdata = _appdata()
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    roots = _config_bases() + [
        os.path.join(home, ".ssh"),
        os.path.join(home, ".aws"),
        os.path.join(home, ".gnupg"),
        os.path.join(appdata, "Microsoft", "Credentials"),
        os.path.join(appdata, "Microsoft", "Protect"),
        os.path.join(appdata, "Microsoft", "Vault"),
        os.path.join(local, "Microsoft", "Credentials"),
        os.path.join(local, "Microsoft", "Vault"),
        os.path.join(appdata, "Mozilla"),
        os.path.join(appdata, "Opera Software"),
        os.path.join(local, "Google", "Chrome", "User Data"),
        os.path.join(local, "Microsoft", "Edge", "User Data"),
        os.path.join(local, "BraveSoftware"),
    ]
    if write:
        roots += [
            os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows",
            os.environ.get("ProgramFiles") or r"C:\Program Files",
            os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)",
        ]
    return {_resolved_key(root) for root in roots}


def _output_roots() -> set[str]:
    """The folders generated files are written to, resolved. Readable, never writable through a tool."""
    return {
        _resolved_key(os.path.join(base, folder))
        for base in _config_bases()
        for folder in _OUTPUT_FOLDERS
    }


def _resolved_key(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _inside(name: str, root: str) -> bool:
    return name == root or name.startswith(root + os.sep)


def _blocked(name: str) -> bool:
    # Substring matching, as config.DEFAULT_BLOCKED documents. Config.is_blocked
    # matches dotted patterns by suffix only, which misses ".env.local"; this
    # check is the stricter one.
    lowered = name.lower()
    return any(pattern in lowered for pattern in _BLOCKED)
