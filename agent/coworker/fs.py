"""Filesystem exploration primitives exposed to the model as tools.

There is no pre-built index and no database. The model walks the disk the way
a person would - list the drives, look inside a folder, ask which one - and
each walk is bounded by a wall-clock deadline so a 400 GB drive can never hang
a reply. Directories the model has been useful in before get visited first,
which is what makes the assistant feel like it is learning the layout.
"""
from __future__ import annotations

import os
import string
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .extract import SUPPORTED, extract
from .textutil import score_expanded

# Noise that is never a user document. Matched case-insensitively against
# each path component.
SKIP_DIRS = {
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "appdata", "recovery",
    "perflogs", "node_modules", ".git", ".svn", "__pycache__", ".venv",
    "venv", "env", ".cache", "temp", "tmp", "cache", "onedrivetemp",
    "msocache", "intel", "nvidia", ".gradle", ".m2", "site-packages",
    "dist-packages", ".idea", ".vscode", "steamapps", "$windows.~bt",
}

# Extensions worth scanning for when SEARCHING. A directory listing does not
# use this - "what is on my Desktop" must answer with what is actually there.
DOC_EXT = SUPPORTED | {
    ".doc", ".xls", ".ppt", ".odt", ".ods", ".odp", ".pages", ".numbers",
    ".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff",  # scans count
    ".zip", ".rar", ".7z",
    # A real Desktop is mostly shortcuts. Leaving these out made 15 of 19
    # files on the user's own Desktop invisible, including the ones they
    # asked for by name.
    ".lnk", ".url", ".appref-ms",
}

# Never worth showing anyone.
HIDE_ALWAYS = {".tmp", ".crdownload", ".part", ".lock"}
HIDE_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}

# Windows knows where these live, including when OneDrive has moved them.
# Guessing "C:\\Users\\<name>\\Desktop" is wrong on exactly the machines this
# project targets - the user's own Desktop is under OneDrive.
_KNOWN_FOLDER_GUIDS = {
    "Desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "Documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "Downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "Pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "Music": "4BD8D571-6D19-48D3-BE97-422220080E43",
    "Videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
}


# What people here actually call these folders. Windows reports them in
# English even on a Russian install, so "ish stoli" or "рабочий стол" would
# otherwise find nothing.
FOLDER_ALIASES = {
    "Desktop": ("ish stoli", "ishstoli", "rabochiy stol", "рабочий стол", "stol"),
    "Documents": ("hujjatlar", "hujjat", "dokumenti", "документы", "mening hujjatlarim"),
    "Downloads": ("yuklab olingan", "yuklamalar", "zagruzki", "загрузки", "yuklangan"),
    "Pictures": ("rasmlar", "rasm", "suratlar", "izobrajeniya", "изображения", "foto"),
    "Music": ("musiqa", "muzika", "музыка"),
    "Videos": ("video", "videolar", "видео"),
}


def known_folders() -> dict[str, str]:
    """{"Desktop": "C:/Users/.../OneDrive/Desktop", ...} - only ones that exist."""
    if os.name != "nt":
        home = Path.home()
        return {n: str(home / n) for n in ("Desktop", "Documents", "Downloads")
                if (home / n).is_dir()}

    import ctypes
    import uuid

    out: dict[str, str] = {}
    for name, guid in _KNOWN_FOLDER_GUIDS.items():
        try:
            buf = ctypes.c_wchar_p()
            raw = ctypes.create_string_buffer(uuid.UUID(guid).bytes_le)
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(raw), 0, None, ctypes.byref(buf)
            ) == 0 and buf.value and os.path.isdir(buf.value):
                out[name] = buf.value
        except Exception:
            continue
    return out

DEFAULT_DEADLINE = 8.0
MAX_DEPTH = 7


@dataclass
class Hit:
    path: str
    name: str
    size: int
    mtime: float
    score: float = 0.0

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "size": _human_size(self.size),
            "modified": datetime.fromtimestamp(self.mtime).strftime("%Y-%m-%d"),
            "score": round(self.score, 2),
        }


# ------------------------------------------------------------------- drives

def list_drives() -> list[dict]:
    """Fixed drives on Windows, a few sensible roots elsewhere."""
    out: list[dict] = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if not os.path.exists(root):
                continue
            try:
                usage = os.statvfs(root) if hasattr(os, "statvfs") else None
            except Exception:
                usage = None
            entry = {"path": root, "label": _drive_label(root)}
            if usage:
                entry["free"] = _human_size(usage.f_bavail * usage.f_frsize)
            else:
                try:
                    import shutil
                    entry["free"] = _human_size(shutil.disk_usage(root).free)
                except Exception:
                    pass
            out.append(entry)
    else:
        home = Path.home()
        for p in (home, home / "Documents", home / "Desktop", Path("/")):
            if p.exists():
                out.append({"path": str(p), "label": p.name or "root"})
    return out


def _drive_label(root: str) -> str:
    if os.name != "nt":
        return ""
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(261)
        ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), buf, 261, None, None, None, None, 0
        )
        return buf.value or ""
    except Exception:
        return ""


# ---------------------------------------------------------------- listing

def list_dir(path: str, limit: int = 60) -> dict:
    """One directory level: folders first, then documents, newest first."""
    p = Path(path)
    if not p.exists():
        return {"error": f"Topilmadi: {path}"}
    if not p.is_dir():
        return {"error": f"Bu papka emas: {path}"}

    dirs: list[dict] = []
    files: list[Hit] = []
    try:
        with os.scandir(p) as it:
            for entry in it:
                try:
                    if entry.name.startswith(".") or _is_hidden(entry):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.lower() in SKIP_DIRS:
                            continue
                        dirs.append({"path": entry.path, "name": entry.name})
                    elif entry.is_file(follow_symlinks=False):
                        # A listing answers "what is in this folder", so it
                        # shows what is there. Filtering to document types
                        # here is how a Desktop of shortcuts came back looking
                        # almost empty.
                        ext = os.path.splitext(entry.name)[1].lower()
                        if ext in HIDE_ALWAYS or entry.name.lower() in HIDE_NAMES:
                            continue
                        st = entry.stat()
                        files.append(Hit(entry.path, entry.name, st.st_size, st.st_mtime))
                except (OSError, PermissionError):
                    continue
    except PermissionError:
        return {"error": f"Ruxsat yo'q: {path}"}

    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f.mtime, reverse=True)
    return {
        "path": str(p),
        "folders": dirs[:limit],
        "files": [f.as_dict() for f in files[:limit]],
        "truncated": len(dirs) > limit or len(files) > limit,
    }


# ----------------------------------------------------------------- searching

def find_files(
    query: str,
    roots: list[str],
    *,
    limit: int = 15,
    deadline: float = DEFAULT_DEADLINE,
    min_score: float = 0.35,
    priority: list[str] | None = None,
) -> dict:
    """Score every reachable filename (and its folder path) against ``query``.

    ``priority`` folders are walked first so previously useful locations
    surface before a full-disk sweep burns the time budget.
    """
    stop_at = time.monotonic() + deadline
    ordered = _order_roots(roots, priority)
    hits: list[Hit] = []
    scanned = 0
    exhausted = True

    for root in ordered:
        for dirpath, filename, st in _walk(root, stop_at):
            scanned += 1
            # The folder name usually carries as much meaning as the file name.
            haystack = os.path.join(os.path.basename(dirpath), filename)
            s = score_expanded(query, haystack)
            if s < min_score:
                continue
            hits.append(Hit(os.path.join(dirpath, filename), filename, st.st_size, st.st_mtime, s))
        if time.monotonic() > stop_at:
            exhausted = False
            break

    hits = _rank(hits)[:limit]
    return {
        "query": query,
        "scanned": scanned,
        "complete": exhausted,
        "results": [h.as_dict() for h in hits],
    }


def find_folders(
    query: str,
    roots: list[str],
    *,
    limit: int = 10,
    deadline: float = DEFAULT_DEADLINE,
    min_score: float = 0.5,
    priority: list[str] | None = None,
) -> dict:
    """Locate a FOLDER by name.

    find_files only ever returned files, so "what is on my Desktop" had no way
    to resolve "Desktop" to a path and turned into a long guessing game down
    C:\\Users. Well-known folders are answered from Windows itself first,
    which is both instant and correct when OneDrive has redirected them.
    """
    from .textutil import normalize

    hits: list[tuple[float, str, str]] = []
    wanted = normalize(query)

    for name, path in known_folders().items():
        candidates = [normalize(name)] + [normalize(a) for a in FOLDER_ALIASES.get(name, ())]
        if wanted and any(wanted in c or c in wanted for c in candidates if c):
            hits.append((1.0, name, path))

    stop_at = time.monotonic() + deadline
    seen = {h[2].lower() for h in hits}
    for root in _order_roots(roots, priority):
        for dirpath in _walk_dirs(root, stop_at):
            if dirpath.lower() in seen:
                continue
            s = score_expanded(query, os.path.basename(dirpath))
            if s >= min_score:
                seen.add(dirpath.lower())
                hits.append((s, os.path.basename(dirpath), dirpath))
        if time.monotonic() > stop_at:
            break

    hits.sort(key=lambda h: h[0], reverse=True)
    return {
        "query": query,
        "results": [
            {"name": n, "path": p, "score": round(s, 2)} for s, n, p in hits[:limit]
        ],
    }


def _walk_dirs(root: str, stop_at: float):
    """Directory-only walk, same guards as :func:`_walk`."""
    if not os.path.isdir(root):
        return
    base_depth = str(root).rstrip("\\/").count(os.sep)
    stack = [str(root)]
    while stack:
        if time.monotonic() > stop_at:
            return
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        if entry.name.startswith(".") or _is_hidden(entry):
                            continue
                        if entry.name.lower() in SKIP_DIRS:
                            continue
                        yield entry.path
                        if entry.path.count(os.sep) - base_depth < MAX_DEPTH:
                            stack.append(entry.path)
                    except (OSError, PermissionError):
                        continue
        except (PermissionError, OSError, FileNotFoundError):
            continue


def search_in_files(
    query: str,
    candidates: list[str],
    *,
    limit: int = 8,
    deadline: float = DEFAULT_DEADLINE,
) -> dict:
    """Second pass: look *inside* the shortlist for the query words."""
    from .textutil import expand

    stop_at = time.monotonic() + deadline
    terms = expand(query)
    out: list[dict] = []
    for path in candidates:
        if time.monotonic() > stop_at or len(out) >= limit:
            break
        text = extract(path, limit=40_000)
        if not text:
            continue
        low = " ".join(t for t in _norm_words(text))
        found = [t for t in terms if t in low]
        if not found:
            continue
        out.append({
            "path": path,
            "matched": found,
            "snippet": _snippet(text, found[0]),
        })
    return {"query": query, "results": out}


def recent_files(
    roots: list[str],
    *,
    days: int = 30,
    limit: int = 20,
    deadline: float = DEFAULT_DEADLINE,
    priority: list[str] | None = None,
) -> dict:
    """Documents touched recently - answers "the one I was working on"."""
    stop_at = time.monotonic() + deadline
    cutoff = time.time() - days * 86400
    hits: list[Hit] = []
    for root in _order_roots(roots, priority):
        for dirpath, filename, st in _walk(root, stop_at):
            if st.st_mtime >= cutoff:
                hits.append(Hit(os.path.join(dirpath, filename), filename, st.st_size, st.st_mtime))
        if time.monotonic() > stop_at:
            break
    hits.sort(key=lambda h: h.mtime, reverse=True)
    return {"days": days, "results": [h.as_dict() for h in hits[:limit]]}


def preview_file(path: str, chars: int = 1200) -> dict:
    p = Path(path)
    if not p.is_file():
        return {"error": f"Fayl topilmadi: {path}"}
    st = p.stat()
    text = extract(p, limit=max(chars, 2000))
    return {
        "path": str(p),
        "size": _human_size(st.st_size),
        "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "text": text[:chars] if text else "",
        "readable": bool(text),
    }


# ------------------------------------------------------------------ internals

def _walk(root: str, stop_at: float):
    """Depth-limited, deadline-aware walk yielding (dir, name, stat)."""
    root = str(root)
    if not os.path.isdir(root):
        return
    base_depth = root.rstrip("\\/").count(os.sep)
    stack = [root]
    while stack:
        if time.monotonic() > stop_at:
            return
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.name.startswith(".") or _is_hidden(entry):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() in SKIP_DIRS:
                                continue
                            if entry.path.count(os.sep) - base_depth < MAX_DEPTH:
                                stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            if os.path.splitext(entry.name)[1].lower() in DOC_EXT:
                                yield current, entry.name, entry.stat()
                    except (OSError, PermissionError):
                        continue
        except (PermissionError, OSError, FileNotFoundError):
            continue


def _order_roots(roots: list[str], priority: list[str] | None) -> list[str]:
    """Known-good folders first, then the rest, without duplicates."""
    ordered: list[str] = []
    for p in priority or []:
        if os.path.isdir(p) and p not in ordered:
            ordered.append(p)
    for r in roots:
        if os.path.isdir(r) and not any(_within(r, o) for o in ordered):
            ordered.append(r)
    return ordered


def _within(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(child), os.path.abspath(parent)]) == os.path.abspath(parent)
    except ValueError:  # different drives
        return False


def _rank(hits: list[Hit]) -> list[Hit]:
    """Blend name relevance with recency - ties go to the newer file."""
    if not hits:
        return []
    now = time.time()
    for h in hits:
        age_days = max((now - h.mtime) / 86400, 0)
        # Full bonus under a week old, fading to nothing after ~2 years.
        recency = max(0.0, 1.0 - age_days / 730) * 0.12
        h.score = min(1.0, h.score + recency)
    hits.sort(key=lambda h: (h.score, h.mtime), reverse=True)
    return hits


def _norm_words(text: str) -> list[str]:
    from .textutil import tokens
    return tokens(text[:40_000], do_stem=True)


def _snippet(text: str, term: str, width: int = 160) -> str:
    from .textutil import translit

    flat = translit(text)
    idx = flat.find(term)
    if idx < 0:
        return text[:width].replace("\n", " ")
    start = max(0, idx - width // 3)
    return ("..." if start else "") + text[start : start + width].replace("\n", " ") + "..."


def _is_hidden(entry: os.DirEntry) -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x2)
    except (OSError, AttributeError):
        return False


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# Public alias - other modules format sizes for the user too.
human_size = _human_size
