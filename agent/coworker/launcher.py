"""Launch installed applications by name, and refuse anything that runs code.

The agent kept claiming it could open programs and then could not: there was no
tool for it. This is that tool.

Shortcuts come from the Start Menu and the desktop, where Windows itself looks.
Matching reuses the cross-script scorer the file search uses, so "telegram",
"телеграм" and a partial name all resolve. Launching is os.startfile on the
shortcut, the same thing a double-click does, so the app keeps its own arguments
and working directory.

The index is a trust boundary. open_app runs without a tap when resolve_exact
finds the name in it, so an entry that starts a shell, an interpreter or a script
host must never be there, whatever it is called. Three checks keep it out:

  the query is refused when it names such a program, so a model cannot ask for
  cmd by a fuzzy name either;

  the shortcut's own name is refused the same way;

  the shortcut's bytes are searched for those programs, because a shortcut named
  "Командная строка" still points at cmd.exe. Paths in a .lnk are stored as ASCII
  or UTF-16LE, so both are searched.

Placeholder files (OneDrive cloud files) are never read: opening one downloads it.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import subprocess
import time
import unicodedata

from .textutil import normalize, score

log = logging.getLogger("launcher")

# Programs that run commands or code. wt, wsl and bash are shells too (Windows
# Terminal, the Linux subsystem's launcher, bash) and are refused alongside the
# owner's list. Mintty (Git Bash's terminal) and the Linux distribution launchers
# (ubuntu.exe and the rest) each open a shell with no command the owner wrote.
_SHELL_STEMS = (
    "cmd", "powershell", "pwsh", "wscript", "cscript", "mshta", "rundll32",
    "regsvr32", "certutil", "bitsadmin", "msiexec", "reg", "sc", "schtasks",
    "wt", "wsl", "wslhost", "wslconfig", "bash", "mintty",
)
_DISTRO = (
    r"ubuntu[0-9]*|debian|kali|opensuse[a-z0-9]*|archlinux|alpine|almalinux|oraclelinux|fedora"
)
_PYTHON = r"python[0-9.]*w?"          # python, pythonw, python3, python3.13
_REFUSED_WORD = re.compile(r"(?:" + "|".join(_SHELL_STEMS) + "|" + _DISTRO + "|" + _PYTHON + ")")
_REFUSED_PHRASES = ("command prompt", "командная строка", "windows terminal", "git bash")

# Matched against lower-cased bytes. The lookarounds keep "misc.exe" from matching "sc.exe".
# Distribution file names can carry a version ("ubuntu2204.exe") or a dotted release
# ("opensuse-leap-15.5.exe"), and Git Bash's launcher is "git-bash.exe".
_EXE_RX = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(_SHELL_STEMS) + "|" + _PYTHON + "|git-bash|"
    + r"ubuntu[0-9]*|debian|kali|opensuse[-a-z0-9.]*|archlinux|alpine|almalinux|oraclelinux|fedora"
    + r")\.exe(?![a-z0-9])"
)
# Script types run by a shell host. The lookahead only: the dot follows the name.
_SCRIPT_RX = re.compile(r"\.(?:bat|cmd|ps1|vbs|js|wsf|hta|msi)(?![a-z0-9])")

# System apps that ship without a Start Menu shortcut. Fixed commands only: a
# model-supplied name never selects an executable from PATH.
_SYSTEM_APPS = {
    "notepad": "notepad.exe", "bloknot": "notepad.exe",
    "calculator": "calc.exe", "kalkulyator": "calc.exe", "calc": "calc.exe",
    "paint": "mspaint.exe", "explorer": "explorer.exe",
    "kompyuter": "explorer.exe", "task manager": "taskmgr.exe",
    "dispetcher": "taskmgr.exe", "settings": "ms-settings:",
    "sozlamalar": "ms-settings:", "control": "control.exe",
    "boshqaruv paneli": "control.exe",
}

# Shortcuts that open an uninstaller, a readme, or a website rather than the
# app itself - never what "open X" means.
_SKIP = ("uninstall", "readme", "website", "homepage", "help",
         "o'chirish", "удалить", "деинсталл")

_REFUSAL_TEXT = {
    "arg_invalid": "Dastur nomini aniq ayting.",
    "hard_deny": "Bu vosita ochilmaydi: buyruq qatori yoki skript ishga tushiradi.",
}

_MAX_SHORTCUT_BYTES = 64 * 1024
_CACHE_TTL = 120.0
# FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
_PLACEHOLDER_ATTRS = 0x1000 | 0x40000 | 0x400000

_cache: tuple[tuple[str, ...], float, dict[str, str]] | None = None


# ------------------------------------------------------------------- refusal

def _words(text: str) -> list[str]:
    """Comparable words of a query. Format characters go first, so "cm​d" is still "cmd"."""
    visible = "".join(ch for ch in (text or "") if unicodedata.category(ch) != "Cf")
    return re.findall(r"[^\W_]+", normalize(visible))


_PHRASES = tuple(" ".join(_words(p)) for p in _REFUSED_PHRASES)


def refusal_code(query: str) -> str | None:
    """Why a program name must not be used, or None when it may be.

    "arg_invalid" for an empty name; "hard_deny" for a shell, an interpreter or a
    script host, in any spelling the scorer would otherwise match.
    """
    words = _words(query)
    if not words:
        return "arg_invalid"
    joined = " " + " ".join(words) + " "
    if any(_REFUSED_WORD.fullmatch(w) for w in words):
        return "hard_deny"
    if any(f" {p} " in joined for p in _PHRASES):
        return "hard_deny"
    return None


# --------------------------------------------------------------------- index

def _shortcut_roots() -> list[str]:
    """Start Menu and desktop folders. Only roots whose base variable is set: a relative
    fallback would scan whatever folder the agent happens to run from."""
    env = os.environ
    pairs = (
        (env.get("APPDATA"), r"Microsoft\Windows\Start Menu\Programs"),
        (env.get("ProgramData"), r"Microsoft\Windows\Start Menu\Programs"),
        (env.get("USERPROFILE"), os.path.join("OneDrive", "Desktop")),
        (env.get("USERPROFILE"), "Desktop"),
        (env.get("PUBLIC"), "Desktop"),
    )
    return [os.path.join(base, rel) for base, rel in pairs if base]


def _target_refused(data: bytes) -> bool:
    """True when the shortcut's bytes name a shell, an interpreter or a script.

    UTF-16LE text reads back as every second byte, at an even or an odd offset,
    so the raw bytes and both halves are searched.
    """
    for view in (data, data[0::2], data[1::2]):
        text = view.decode("latin-1").lower()
        if _EXE_RX.search(text) or _SCRIPT_RX.search(text):
            return True
    return False


def _is_safe_shortcut(path: str) -> bool:
    """A shortcut may be indexed only when it is local and its target is not a shell or script."""
    try:
        if getattr(os.stat(path), "st_file_attributes", 0) & _PLACEHOLDER_ATTRS:
            return False
        with open(path, "rb") as fh:
            data = fh.read(_MAX_SHORTCUT_BYTES + 1)
    except OSError:
        return False
    return len(data) <= _MAX_SHORTCUT_BYTES and not _target_refused(data)


def _scan(roots: tuple[str, ...]) -> dict[str, str]:
    apps: dict[str, str] = {}
    for root in roots:
        for path in glob.glob(os.path.join(glob.escape(root), "**", "*.lnk"), recursive=True):
            name = os.path.splitext(os.path.basename(path))[0]
            if any(s in name.lower() for s in _SKIP) or refusal_code(name):
                continue
            if not _is_safe_shortcut(path):
                log.debug("shortcut kept out of the index: %s", name)
                continue
            # Prefer the shortest path for a given name (usually the top-level
            # "Google Chrome" over a nested variant).
            if name not in apps or len(path) < len(apps[name]):
                apps[name] = path
    return apps


def _index() -> dict[str, str]:
    """{display name: .lnk path}. Cached for two minutes; installs are rare."""
    global _cache
    roots = tuple(r for r in _shortcut_roots() if os.path.isdir(r))
    now = time.monotonic()
    if _cache is not None and _cache[0] == roots and now - _cache[1] < _CACHE_TTL:
        return _cache[2]
    apps = _scan(roots)
    _cache = (roots, now, apps)
    return apps


# ------------------------------------------------------------------ matching

def find(query: str, limit: int = 5) -> list[dict]:
    """Ranked shortcut matches for a spoken app name. A refused query matches nothing."""
    if refusal_code(query):
        return []
    scored = []
    q = normalize(query)
    for name, path in _index().items():
        s = score(query, name)
        # A clean prefix or substring match is worth a lot for app names, which are
        # short and distinctive ("telegram" -> "Telegram").
        n = normalize(name)
        if n == q:
            s = 1.0
        elif n.startswith(q) or q in n:
            s = max(s, 0.9)
        if s >= 0.4:
            scored.append((s, name, path))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"name": n, "path": p, "score": round(s, 2)} for s, n, p in scored[:limit]]


def resolve_exact(name: str) -> dict | None:
    """The indexed shortcut whose display name equals ``name`` after normalisation, or None.

    This is the only lookup open_app may relax on: a partial or fuzzy name is never
    enough to skip the owner's tap.
    """
    if refusal_code(name):
        return None
    key = normalize(name)
    for display, path in _index().items():
        if normalize(display) == key:
            return {"name": display, "path": path}
    return None


# --------------------------------------------------------------------- launch

def launch(query: str) -> dict:
    """Open the best-matching application.

    An exact shortcut name always wins. A close second is not guessed at: the
    candidates come back and nothing is launched.
    """
    if os.name != "nt":
        return {"error": "Faqat Windows"}
    code = refusal_code(query)
    if code:
        return {"error": _REFUSAL_TEXT[code], "code": code}

    exact = resolve_exact(query)
    if exact is not None:
        return _start(exact["path"], exact["name"])

    matches = find(query, limit=5)
    if not matches:
        sys_result = _launch_system(query)
        if sys_result is not None:
            return sys_result
        return {"error": f"«{query}» nomli dastur topilmadi.", "code": "arg_invalid"}

    best = matches[0]
    if len(matches) > 1 and best["score"] - matches[1]["score"] < 0.12 and best["score"] < 0.95:
        return {"ambiguous": True, "options": [m["name"] for m in matches[:4]], "query": query}
    return _start(best["path"], best["name"])


def _start(path: str, name: str) -> dict:
    try:
        os.startfile(path)
    except Exception as exc:
        return {"error": f"Ochib bo'lmadi: {exc}"}
    return {"ok": True, "opened": name}


def _launch_system(query: str) -> dict | None:
    """Open a built-in Windows app from the fixed table. None when the name is not in it."""
    q = normalize(query)
    target = next(
        (cmd for name, cmd in _SYSTEM_APPS.items() if normalize(name) == q or q in normalize(name)),
        None,
    )
    if target is None:
        return None
    try:
        if target.endswith(":"):        # a ms-settings: style protocol
            os.startfile(target)
        else:
            subprocess.Popen([target], shell=False)
    except Exception as exc:
        return {"error": f"Ochib bo'lmadi: {exc}"}
    return {"ok": True, "opened": query}
