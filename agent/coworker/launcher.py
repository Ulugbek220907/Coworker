"""Launch installed applications by name.

The agent kept claiming it could open programs and then could not - there was
no tool for it, so "open Telegram" and "open Antigravity" both dead-ended. This
is that tool.

It works off the Start Menu and desktop shortcuts, which is where Windows
itself looks: ~160 .lnk files on a normal machine, covering everything the
user actually has installed. Matching reuses the same cross-script scorer the
file search uses, so "telegram", "телеграм" and a partial name all resolve.
Launching is os.startfile on the shortcut - the same thing double-clicking it
does - so it inherits the app's own arguments and working directory.
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
import time

from .textutil import normalize, score

log = logging.getLogger("launcher")

# System apps that ship without a Start Menu shortcut. Bare command -> what to
# hand the shell. Windows resolves most of these through App Paths / PATH.
_SYSTEM_APPS = {
    "notepad": "notepad.exe", "bloknot": "notepad.exe",
    "calculator": "calc.exe", "kalkulyator": "calc.exe", "calc": "calc.exe",
    "paint": "mspaint.exe", "cmd": "cmd.exe", "terminal": "wt.exe",
    "powershell": "powershell.exe", "explorer": "explorer.exe",
    "kompyuter": "explorer.exe", "task manager": "taskmgr.exe",
    "dispetcher": "taskmgr.exe", "settings": "ms-settings:",
    "sozlamalar": "ms-settings:", "control": "control.exe",
    "boshqaruv paneli": "control.exe",
}

# Shortcuts that open an uninstaller, a readme, or a website rather than the
# app itself - never what "open X" means.
_SKIP = ("uninstall", "readme", "website", "homepage", "help",
         "o'chirish", "удалить", "деинсталл")

_cache: dict[str, str] | None = None
_cached_at = 0.0
_CACHE_TTL = 120.0


def _shortcut_roots() -> list[str]:
    env = os.environ.get
    return [
        os.path.join(env("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(env("ProgramData", ""), r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(env("USERPROFILE", ""), "OneDrive", "Desktop"),
        os.path.join(env("USERPROFILE", ""), "Desktop"),
        env("PUBLIC", "") and os.path.join(env("PUBLIC", ""), "Desktop"),
    ]


def _index() -> dict[str, str]:
    """{display name: shortcut path}. Cached briefly - installs are rare."""
    global _cache, _cached_at
    now = time.monotonic()
    if _cache is not None and now - _cached_at < _CACHE_TTL:
        return _cache

    apps: dict[str, str] = {}
    for root in _shortcut_roots():
        if not root or not os.path.isdir(root):
            continue
        for pattern in ("*.lnk", "*.url", "*.appref-ms"):
            for path in glob.glob(os.path.join(root, "**", pattern), recursive=True):
                name = os.path.splitext(os.path.basename(path))[0]
                low = name.lower()
                if any(s in low for s in _SKIP):
                    continue
                # Prefer the shortest path for a given name (usually the
                # top-level "Google Chrome" over a nested variant).
                if name not in apps or len(path) < len(apps[name]):
                    apps[name] = path
    _cache, _cached_at = apps, now
    return apps


def find(query: str, limit: int = 5) -> list[dict]:
    """Ranked shortcut matches for a spoken app name."""
    apps = _index()
    scored = []
    q = normalize(query)
    for name, path in apps.items():
        s = score(query, name)
        # A clean prefix/substring match is worth a lot for app names, which
        # are short and distinctive ("telegram" -> "Telegram").
        n = normalize(name)
        if n == q:
            s = 1.0
        elif n.startswith(q) or q in n:
            s = max(s, 0.9)
        if s >= 0.4:
            scored.append((s, name, path))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"name": n, "path": p, "score": round(s, 2)} for s, n, p in scored[:limit]]


def launch(query: str) -> dict:
    """Open the best-matching application.

    Returns the match plus a short list of alternatives, so an ambiguous name
    can be turned into a follow-up question rather than a wrong guess.
    """
    if os.name != "nt":
        return {"error": "Faqat Windows"}

    matches = find(query, limit=5)
    if not matches:
        # No shortcut - try a system app or a bare executable on PATH.
        sys_result = _launch_system(query)
        if sys_result is not None:
            return sys_result
        return {"error": f"«{query}» nomli dastur topilmadi."}

    best = matches[0]
    # If the top two are close, do not guess - let the caller ask.
    if len(matches) > 1 and best["score"] - matches[1]["score"] < 0.12 and best["score"] < 0.95:
        return {
            "ambiguous": True,
            "options": [m["name"] for m in matches[:4]],
            "query": query,
        }

    try:
        os.startfile(best["path"])
    except Exception as exc:
        return {"error": f"Ochib bo'lmadi: {exc}"}
    return {"ok": True, "opened": best["name"]}


def _launch_system(query: str) -> dict | None:
    """Open a built-in Windows app or a bare command on PATH. None = no match."""
    q = normalize(query)
    command = None
    for name, cmd in _SYSTEM_APPS.items():
        if normalize(name) == q or q in normalize(name):
            command = cmd
            break
    if command is None:
        exe = shutil.which(query) or shutil.which(query + ".exe")
        if exe:
            command = exe
    if command is None:
        return None

    try:
        if command.endswith(":"):        # a ms-settings: style protocol
            os.startfile(command)
        else:
            subprocess.Popen(command, shell=False)
        return {"ok": True, "opened": query}
    except Exception as exc:
        return {"error": f"Ochib bo'lmadi: {exc}"}


def launch_exact(name: str) -> dict:
    """Open a specific shortcut the user picked from the options."""
    apps = _index()
    path = apps.get(name)
    if path is None:
        for known, p in apps.items():
            if normalize(known) == normalize(name):
                path = p
                break
    if path is None:
        return {"error": f"«{name}» topilmadi."}
    try:
        os.startfile(path)
    except Exception as exc:
        return {"error": f"Ochib bo'lmadi: {exc}"}
    return {"ok": True, "opened": name}
