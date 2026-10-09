"""Keystrokes and the clipboard, delivered only to a window that was verified in front.

Keystrokes reach whatever window has focus, so a keystroke sent blind goes to
whatever the owner happens to be using. Every send here therefore runs inside
one UIA job: the job makes the target the foreground window, checks that it
really is, and only then sends. When the check fails nothing is sent.

Text is typed by pasting, because a paste carries the exact string, Unicode
included, in one step. A paste overwrites the owner's clipboard, so the whole
clipboard is saved in every format first and restored afterwards. If the
clipboard cannot be saved completely, nothing is pasted at all.
"""
from __future__ import annotations

import ctypes
import logging
import time
from typing import Iterable, Protocol

from . import uia

log = logging.getLogger("keys")

MAX_TEXT = 2000                       # longer text is refused, never truncated
MAX_CLIPBOARD_BYTES = 64 * 1024 * 1024
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
OPEN_TRIES = 20

# Clipboard formats whose data is a GDI handle, not bytes, so they cannot be
# copied out and back. Windows always offers a byte form (CF_DIB, CF_DIBV5) of a
# bitmap alongside CF_BITMAP, and that form is saved instead.
_GDI_HANDLE_FORMATS = frozenset({
    2,       # CF_BITMAP
    3,       # CF_METAFILEPICT
    9,       # CF_PALETTE
    14,      # CF_ENHMETAFILE
    0x0080,  # CF_OWNERDISPLAY
    0x0082,  # CF_DSPBITMAP
    0x0083,  # CF_DSPMETAFILEPICT
    0x008E,  # CF_DSPENHMETAFILE
})
_GDI_OBJECT_RANGE = range(0x0300, 0x0400)

# Modifier and key names. The canonical spelling is the one used everywhere else;
# aliases (including the Uzbek words the owner may type) are folded onto it.
_MOD_VK = {"ctrl": "VK_CONTROL", "alt": "VK_MENU", "shift": "VK_SHIFT", "win": "VK_LWIN"}
_MOD_ORDER = ("ctrl", "alt", "shift", "win")
_MOD_ALIASES = {"control": "ctrl", "super": "win", "cmd": "win", "meta": "win", "windows": "win"}

_KEY_VK = {
    "enter": "VK_RETURN", "tab": "VK_TAB", "escape": "VK_ESCAPE", "space": "VK_SPACE",
    "backspace": "VK_BACK", "delete": "VK_DELETE", "insert": "VK_INSERT",
    "home": "VK_HOME", "end": "VK_END", "pageup": "VK_PRIOR", "pagedown": "VK_NEXT",
    "up": "VK_UP", "down": "VK_DOWN", "left": "VK_LEFT", "right": "VK_RIGHT",
    "printscreen": "VK_SNAPSHOT", "capslock": "VK_CAPITAL",
}
_KEY_VK.update({f"f{i}": f"VK_F{i}" for i in range(1, 25)})
_KEY_ALIASES = {
    "return": "enter", "kirit": "enter", "esc": "escape", "probel": "space",
    "boshliq": "space", "back": "backspace", "del": "delete", "ochir": "delete",
    "ins": "insert", "pgup": "pageup", "pgdn": "pagedown", "prtsc": "printscreen",
}

# Combinations that close, discard, lock or delete something. Ordinary shortcuts
# run straight through; these are proposed to the owner first.
DANGEROUS = frozenset({
    "alt+f4", "ctrl+f4", "ctrl+w", "ctrl+q", "ctrl+shift+w", "ctrl+shift+q",
    "alt+shift+f4", "win+l", "ctrl+alt+delete", "shift+delete",
    "ctrl+shift+delete", "win+d", "alt+f7",
})
# Letters whose Ctrl combination is a line break control code (CR for M, LF for J).
_CONTROL_ENTER_KEYS = frozenset({"m", "j"})


def available() -> bool:
    import importlib.util
    return bool(importlib.util.find_spec("uiautomation"))


def status() -> str:
    return "tayyor" if available() else "o'rnatilmagan — pip install uiautomation"


# ------------------------------------------------------------ combinations

def parse_combo(combo: str) -> tuple[tuple[str, ...], str]:
    """Split "Ctrl + Shift + S" into ((ctrl, shift), "s"), with canonical names.

    Raises ValueError with an owner-facing message when the combination has no
    main key, has two main keys, or names a key this module does not know.
    """
    parts = [p.strip().lower() for p in str(combo).split("+")]
    parts = [p for p in parts if p]
    if not parts:
        raise ValueError("bo'sh kombinatsiya")

    mods: set[str] = set()
    main: str | None = None
    for part in parts:
        mod = _MOD_ALIASES.get(part, part)
        if mod in _MOD_VK:
            mods.add(mod)
            continue
        key = _KEY_ALIASES.get(part, part)
        if main is not None:
            raise ValueError(f"bir nechta asosiy tugma: {combo}")
        if key in _KEY_VK or (len(key) == 1 and key.isascii() and key.isalnum()):
            main = key
        else:
            raise ValueError(f"noma'lum tugma: {part}")
    if main is None:
        raise ValueError("asosiy tugma ko'rsatilmagan")
    return tuple(m for m in _MOD_ORDER if m in mods), main


def normalize(combo: str) -> str:
    """"Ctrl + Shift + S" -> "ctrl+shift+s". Raises ValueError for an unknown combination."""
    mods, main = parse_combo(combo)
    return "+".join([*mods, main])


def is_dangerous(combo: str) -> bool:
    """True for combinations that close or delete things, and for anything unparseable.

    An unparseable combination is not sent anyway; treating it as dangerous means
    nothing relaxes it.
    """
    try:
        return normalize(combo) in DANGEROUS
    except ValueError:
        return True


def sends_enter(combo: str) -> bool:
    """True when the combination delivers a line break: Enter itself, or a Ctrl+letter that is the same control code.

    Ctrl+M is carriage return and Ctrl+J is line feed, so a console, a line editor
    or a chat box accepts them exactly as it accepts Enter. An unparseable
    combination is not treated as Enter here; it is never sent either.
    """
    try:
        mods, main = parse_combo(combo)
    except ValueError:
        return False
    return main == "enter" or ("ctrl" in mods and main in _CONTROL_ENTER_KEYS)


# --------------------------------------------------------------- keystrokes

def press(combo: str, handle: int, blocked: Iterable[str] = uia.DEFAULT_BLOCKED_WINDOWS) -> dict:
    """Send one key combination to window `handle`, after verifying it is in front."""
    if not available():
        return {"error": status()}
    if not handle:
        return _no_target()
    try:
        mods, main = parse_combo(combo)
    except ValueError as exc:
        return {"error": str(exc), "code": "arg_invalid"}
    label = "+".join([*mods, main])
    patterns = tuple(blocked)

    def job():
        import uiautomation as auto

        refused = uia.ensure_foreground(handle, patterns) or uia.ensure_not_password_focus(auto)
        if refused:
            return refused
        _tap(auto, _virtual_keys(auto, mods, main))
        return {"ok": True, "pressed": label, "window": _focused_title()}

    return uia.run(job, timeout=30)


def type_text(text: str, handle: int, blocked: Iterable[str] = uia.DEFAULT_BLOCKED_WINDOWS) -> dict:
    """Type literal text into window `handle` by pasting it, after verifying the focus."""
    if not available():
        return {"error": status()}
    if not handle:
        return _no_target()
    text = str(text)
    if not text:
        return {"error": "matn bo'sh", "code": "arg_invalid"}
    if len(text) > MAX_TEXT:
        return {
            "error": f"Matn juda uzun: {len(text)} belgi. Ko'pi bilan {MAX_TEXT} belgi yuborish mumkin.",
            "code": "arg_invalid",
        }
    patterns = tuple(blocked)

    def job():
        import uiautomation as auto

        refused = uia.ensure_foreground(handle, patterns) or uia.ensure_not_password_focus(auto)
        if refused:
            return refused
        return _paste(auto, text)

    return uia.run(job, timeout=40)


def _paste(auto, text: str) -> dict:
    """Paste `text` through the clipboard, then put the owner's clipboard back. Runs on the UIA thread."""
    board = _board()
    saved = snapshot_clipboard(board)
    if saved is None:
        return {"error": "Buferdagi ma'lumotni saqlab bo'lmadi — matn yozilmadi.", "sent": False}
    try:
        if not write_clipboard(board, [(CF_UNICODETEXT, _utf16(text))]):
            return {"error": "Buferga yozib bo'lmadi — matn yozilmadi.", "sent": False}
        time.sleep(0.12)
        _tap(auto, _virtual_keys(auto, ("ctrl",), "v"))
    finally:
        # An Electron editor reads the clipboard asynchronously after ctrl+v; the
        # old content must not come back before that read has happened.
        time.sleep(0.7)
        if not write_clipboard(board, saved):
            log.warning("clipboard could not be restored after a paste")
    return {"ok": True, "typed": len(text), "window": _focused_title()}


def _tap(auto, codes: list) -> None:
    """Hold keys down in order, then release them in reverse - always, even when a press fails.

    A modifier left down would wreck the next thing the owner types, so every
    key that was pressed is released in a finally block.
    """
    down: list = []
    try:
        for code in codes:
            auto.PressKey(code)
            down.append(code)
        time.sleep(0.03)
    finally:
        for code in reversed(down):
            try:
                auto.ReleaseKey(code)
            except Exception:
                log.warning("a key could not be released")


def _virtual_keys(auto, mods: tuple[str, ...], main: str) -> list:
    codes = [getattr(auto.Keys, _MOD_VK[m]) for m in mods]
    codes.append(getattr(auto.Keys, _KEY_VK.get(main) or f"VK_{main.upper()}"))
    return codes


# ---------------------------------------------------------------- clipboard

class ClipboardBoard(Protocol):
    """The clipboard operations this module uses. Every call except open() needs an open board."""

    def open(self) -> bool: ...
    def close(self) -> None: ...
    def formats(self) -> list[int]: ...
    def size(self, fmt: int) -> int | None: ...
    def read(self, fmt: int) -> bytes | None: ...
    def clear(self) -> None: ...
    def write(self, fmt: int, data: bytes) -> bool: ...


def _copyable(fmt: int) -> bool:
    return fmt not in _GDI_HANDLE_FORMATS and fmt not in _GDI_OBJECT_RANGE


def snapshot_clipboard(board: ClipboardBoard, cap: int = MAX_CLIPBOARD_BYTES) -> list[tuple[int, bytes]] | None:
    """Every copyable format on the clipboard, as (format, bytes).

    Returns None when the clipboard cannot be read completely: it cannot be
    opened, a format's data is unavailable, or the total exceeds `cap`. The
    caller must then leave the clipboard alone.
    """
    if not board.open():
        return None
    try:
        items: list[tuple[int, bytes]] = []
        total = 0
        for fmt in board.formats():
            if not _copyable(fmt):
                continue
            size = board.size(fmt)
            if size is None:
                return None
            total += size
            if total > cap:
                return None
            data = board.read(fmt)
            if data is None:
                return None
            items.append((fmt, data))
        return items
    finally:
        board.close()


def write_clipboard(board: ClipboardBoard, items: list[tuple[int, bytes]]) -> bool:
    """Replace the whole clipboard with `items`. An empty list leaves it empty."""
    if not board.open():
        return False
    try:
        board.clear()
        return all(board.write(fmt, data) for fmt, data in items)
    finally:
        board.close()


def read_text(board: ClipboardBoard) -> str | None:
    """The clipboard's text, "" when it holds none, None when it cannot be opened."""
    if not board.open():
        return None
    try:
        data = board.read(CF_UNICODETEXT)
    finally:
        board.close()
    if not data:
        return ""
    return data.decode("utf-16-le", errors="ignore").split("\x00", 1)[0]


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-le") + b"\x00\x00"


def _board() -> ClipboardBoard:
    return _Win32Clipboard()


class _Win32Clipboard:
    """The Windows clipboard through user32 and kernel32. Only used on Windows."""

    def __init__(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.EnumClipboardFormats.argtypes = [ctypes.c_uint]
        user32.EnumClipboardFormats.restype = ctypes.c_uint
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
        kernel32.GlobalSize.restype = ctypes.c_size_t
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        self._user32 = user32
        self._kernel32 = kernel32

    def open(self) -> bool:
        for _ in range(OPEN_TRIES):
            if self._user32.OpenClipboard(None):
                return True
            time.sleep(0.02)                  # another process holds it briefly
        return False

    def close(self) -> None:
        self._user32.CloseClipboard()

    def formats(self) -> list[int]:
        out: list[int] = []
        fmt = 0
        while True:
            fmt = self._user32.EnumClipboardFormats(fmt)
            if not fmt:
                return out
            out.append(int(fmt))

    def size(self, fmt: int) -> int | None:
        handle = self._user32.GetClipboardData(fmt)
        if not handle:
            return None
        return int(self._kernel32.GlobalSize(handle))

    def read(self, fmt: int) -> bytes | None:
        handle = self._user32.GetClipboardData(fmt)
        if not handle:
            return None
        pointer = self._kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.string_at(pointer, int(self._kernel32.GlobalSize(handle)))
        finally:
            self._kernel32.GlobalUnlock(handle)

    def clear(self) -> None:
        self._user32.EmptyClipboard()

    def write(self, fmt: int, data: bytes) -> bool:
        handle = self._kernel32.GlobalAlloc(GMEM_MOVEABLE, max(len(data), 1))
        if not handle:
            return False
        pointer = self._kernel32.GlobalLock(handle)
        if not pointer:
            self._kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(pointer, data, len(data))
        self._kernel32.GlobalUnlock(handle)
        # On success the system owns the memory; on failure it is ours to free.
        if not self._user32.SetClipboardData(fmt, handle):
            self._kernel32.GlobalFree(handle)
            return False
        return True


def clipboard_get() -> dict:
    """The clipboard's text, untouched. The tool layer redacts it before the model sees it."""
    if not available():
        return {"error": status()}

    def job():
        text = read_text(_board())
        if text is None:
            return {"error": "Buferni o'qib bo'lmadi (boshqa ilova band qilgan bo'lishi mumkin)."}
        return {"text": text[:4000], "length": len(text)}

    return uia.run(job, timeout=20)


def clipboard_set(text: str) -> dict:
    """Replace the clipboard with `text`. What it held before is not kept."""
    if not available():
        return {"error": status()}
    text = str(text)
    if len(text) > MAX_TEXT:
        return {
            "error": f"Matn juda uzun: {len(text)} belgi. Ko'pi bilan {MAX_TEXT} belgi.",
            "code": "arg_invalid",
        }

    def job():
        if not write_clipboard(_board(), [(CF_UNICODETEXT, _utf16(text))]):
            return {"error": "Buferga yozib bo'lmadi."}
        return {"ok": True, "length": len(text)}

    return uia.run(job, timeout=20)


# ------------------------------------------------------------------ helpers

def _no_target() -> dict:
    return {
        "error": "Oyna tanlanmagan: avval list_windows yoki read_window bilan handle oling.",
        "code": "arg_invalid",
    }


def _focused_title() -> str:
    """Title of the window that received the input, so the model sees where it actually landed."""
    try:
        import uiautomation as auto

        handle = uia.foreground_handle()
        if not handle:
            return ""
        control = auto.ControlFromHandle(handle)
        return uia._clean(control.Name)[:60] if control else ""
    except Exception:
        return ""
