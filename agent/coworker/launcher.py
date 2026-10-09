"""Launch installed applications by name, and refuse anything that runs code.

The agent kept claiming it could open programs and then could not: there was no
tool for it. This is that tool.

Shortcuts come from the Start Menu and the desktop, where Windows itself looks.
Matching is by words, not by a score. A name resolves to a shortcut only when it
is that shortcut's full name (ignoring case, punctuation and spaces, and reading
Cyrillic as Latin, so "телеграм" is "telegram") or a declared alias of an installed
shortcut. A partial name is never started: it becomes a list the owner chooses
from. Launching is os.startfile on the shortcut, the same thing a double-click
does, so the app keeps its own arguments and working directory.

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

from .textutil import normalize

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


def _key(text: str) -> str:
    """The comparison form of a name: its words, lower-cased and joined by single spaces."""
    return " ".join(_words(text))


_PHRASES = tuple(_key(p) for p in _REFUSED_PHRASES)


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

# A partial name must offer at least this many letters. "tel" finds Telegram; "a" and
# "e" find nothing, which is why they once started notepad through a substring test.
_MIN_PARTIAL = 3

# Words an owner says for "any browser" or "any program". They name a category, not a
# shortcut, so they never resolve to one: "open browser" must not start whichever
# program happens to contain the word. The browser is looked up with default_browser.
_CATEGORY_NAMES = frozenset(_key(w) for w in (
    "browser", "web browser", "internet browser", "browsers", "brauzer",
    "app", "apps", "application", "applications", "program", "programs", "dastur", "ilova",
))

# A product an owner names by a shorter or different title than its shortcut, or whose
# first word is shared with another shortcut ("Chrome" is also Chrome Remote Desktop).
# An alias resolves only to a shortcut that is installed; it never invents one.
_ALIASES = {_key(a): _key(b) for a, b in (
    ("chrome", "Google Chrome"),
    ("edge", "Microsoft Edge"),
    ("ms edge", "Microsoft Edge"),
)}


def _word_matches(query_word: str, name_word: str) -> bool:
    """True when one query word names one word of a shortcut.

    Equal words match. A query word of at least _MIN_PARTIAL letters matches a longer
    word it begins. A query word of five or more letters also matches inside a joined
    word, so "office" finds "OpenOffice".
    """
    if query_word == name_word:
        return True
    if len(query_word) < _MIN_PARTIAL:
        return False
    return name_word.startswith(query_word) or (len(query_word) >= 5 and query_word in name_word)


def _candidates(query: str) -> list[tuple[float, str, str]]:
    """Every shortcut whose words cover every word of the query, best first.

    A shortcut that begins with the whole query ranks above one that merely contains
    its words. A query made only of words shorter than _MIN_PARTIAL covers nothing.
    """
    key = _key(query)
    words = key.split()
    if not words or key in _CATEGORY_NAMES or not any(len(w) >= _MIN_PARTIAL for w in words):
        return []
    found: list[tuple[float, str, str]] = []
    for name, path in _index().items():
        name_key = _key(name)
        name_words = name_key.split()
        if not name_words:
            continue
        if all(any(_word_matches(w, nw) for nw in name_words) for w in words):
            rank = 0.95 if name_key.startswith(key) else 0.8
            found.append((rank, name, path))
    found.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
    return found


def find(query: str, limit: int = 5) -> list[dict]:
    """Shortcuts that could be the program the owner means. Listing only: nothing is started.

    A refused query matches nothing.
    """
    if refusal_code(query):
        return []
    return [
        {"name": name, "path": path, "score": round(rank, 2)}
        for rank, name, path in _candidates(query)[:limit]
    ]


def _result(status: str, options: list[str] | None = None) -> dict:
    return {"status": status, "name": None, "path": None, "options": list(options or [])}


def _exact(name: str, index: dict[str, str]) -> dict:
    return {"status": "exact", "name": name, "path": index[name], "options": []}


def resolve(query: str) -> dict:
    """Decide which installed application the owner's words name, or why that is not possible.

    The status is one of:

      'exact'    the words are one shortcut's full name, or a declared alias of an
                 installed shortcut. This is the only status that may start anything.
      'choose'   several shortcuts are plausible and none is exact. ``options`` holds up
                 to four names for the owner to pick from.
      'none'     nothing is plausible, including a category word such as "browser".
      'refused'  refusal_code(query) is set: a shell, an interpreter, a script host or
                 an empty name.

    Ties are broken by the name alone. The text as typed wins when it is one shortcut's
    name; a normalised name that equals exactly one shortcut's name wins over longer
    shortcuts that merely contain it. Two shortcuts with the same normalised name are a
    choice, never a guess. Nothing is guessed.
    """
    if refusal_code(query):
        return _result("refused")
    key = _key(query)
    if not key or key in _CATEGORY_NAMES:
        return _result("none")
    index = _index()

    # The text exactly as typed separates "Old-Tool" from "Old Tool" when both are installed.
    raw = query.strip()
    if raw in index:
        return _exact(raw, index)

    same = [name for name in index if _key(name) == key]
    if len(same) == 1:
        return _exact(same[0], index)
    if len(same) > 1:
        return _result("choose", sorted(same)[:4])

    target = _ALIASES.get(key)
    if target is not None:
        named = [name for name in index if _key(name) == target]
        if len(named) == 1:
            return _exact(named[0], index)

    candidates = _candidates(query)
    if not candidates:
        return _result("none")
    return _result("choose", [name for _, name, _ in candidates][:4])


def resolve_exact(name: str) -> dict | None:
    """The shortcut when resolve(name) is 'exact', otherwise None.

    open_app relaxes its confirmation only on an exact result, so a partial or fuzzy
    name never skips the owner's tap.
    """
    res = resolve(name)
    if res["status"] != "exact":
        return None
    return {"name": res["name"], "path": res["path"]}


# --------------------------------------------------------------------- launch

def launch(query: str) -> dict:
    """Start the application the owner named, and only that one.

    Only an 'exact' resolution starts. A 'choose' result returns its options and
    launches nothing, so a partial name is never taken for the program meant. The
    built-in table is consulted only when resolve() finds no shortcut at all.
    """
    if os.name != "nt":
        return {"error": "Faqat Windows"}
    res = resolve(query)
    if res["status"] == "refused":
        code = refusal_code(query) or "arg_invalid"
        return {"error": _REFUSAL_TEXT[code], "code": code}
    if res["status"] == "exact":
        return _start(res["path"], res["name"])
    if res["status"] == "choose":
        return {"ambiguous": True, "options": res["options"], "query": query}

    sys_result = _launch_system(query)
    if sys_result is not None:
        return sys_result
    return {"error": f"«{query}» nomli dastur topilmadi.", "code": "not_found"}


def _start(path: str, name: str) -> dict:
    try:
        os.startfile(path)
    except Exception as exc:
        return {"error": f"Ochib bo'lmadi: {exc}"}
    return {"ok": True, "opened": name}


_SYSTEM_MIN_LEN = 4


def _launch_system(query: str) -> dict | None:
    """Open a built-in Windows app from the fixed table. None when the name is not in it.

    The whole query must equal a table name or alias, and be at least four letters.
    A substring test once let "a" or "e" start notepad, and a word inside a longer
    phrase must not start a system tool either.
    """
    key = _key(query)
    if len(key) < _SYSTEM_MIN_LEN:
        return None
    target = next((cmd for name, cmd in _SYSTEM_APPS.items() if _key(name) == key), None)
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
