"""Global keyboard input and the clipboard.

The UIA layer can already type into a *field* it has located. This is the
level below that: keystrokes delivered to whatever window currently has focus,
which is the only way to reach shortcuts (Ctrl+S, Alt+Tab) and applications
whose UI tree is empty.

Because it goes to the focused window and nowhere else, focus is the safety
boundary: the caller is expected to focus the intended window first
(`window_focus`), and the agent's prompt says so. A keystroke sent blind is a
keystroke sent to whatever the user happened to be typing in.

Input is routed through the one UIA worker thread, like everything else that
touches Windows here - it serialises keystrokes so two requests can never
interleave halfway through a combination.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("keys")

MAX_TEXT = 2000

MODIFIERS = {
    "ctrl": "VK_CONTROL", "control": "VK_CONTROL",
    "alt": "VK_MENU",
    "shift": "VK_SHIFT",
    "win": "VK_LWIN", "super": "VK_LWIN", "cmd": "VK_LWIN", "meta": "VK_LWIN",
}

# Friendly names -> virtual-key constant names, in three spellings where the
# user is likely to reach for one.
NAMED_KEYS = {
    "enter": "VK_RETURN", "return": "VK_RETURN", "kirit": "VK_RETURN",
    "tab": "VK_TAB",
    "esc": "VK_ESCAPE", "escape": "VK_ESCAPE",
    "space": "VK_SPACE", "probel": "VK_SPACE", "boshliq": "VK_SPACE",
    "backspace": "VK_BACK", "back": "VK_BACK",
    "delete": "VK_DELETE", "del": "VK_DELETE", "ochir": "VK_DELETE",
    "insert": "VK_INSERT", "ins": "VK_INSERT",
    "home": "VK_HOME", "end": "VK_END",
    "pageup": "VK_PRIOR", "pgup": "VK_PRIOR",
    "pagedown": "VK_NEXT", "pgdn": "VK_NEXT",
    "up": "VK_UP", "down": "VK_DOWN", "left": "VK_LEFT", "right": "VK_RIGHT",
    "printscreen": "VK_SNAPSHOT", "prtsc": "VK_SNAPSHOT",
    "capslock": "VK_CAPITAL",
}
for _i in range(1, 25):
    NAMED_KEYS[f"f{_i}"] = f"VK_F{_i}"

# Combinations that close, discard or lock something. Ordinary shortcuts run
# straight through; these are proposed to the user first.
DANGEROUS = {
    "alt+f4", "ctrl+w", "ctrl+q", "ctrl+shift+w", "ctrl+shift+q",
    "alt+shift+f4", "win+l", "ctrl+alt+delete", "shift+delete",
    "ctrl+shift+delete", "win+d", "alt+f7",
}


def available() -> bool:
    import importlib.util
    return bool(importlib.util.find_spec("uiautomation"))


def status() -> str:
    return "tayyor" if available() else "o'rnatilmagan — pip install uiautomation"


def normalize(combo: str) -> str:
    """"Ctrl + Shift + S" -> "ctrl+shift+s", modifiers in a stable order."""
    parts = [p.strip().lower() for p in str(combo).split("+") if p.strip()]
    mods = [p for p in parts if p in MODIFIERS]
    rest = [p for p in parts if p not in MODIFIERS]
    order = {"ctrl": 0, "control": 0, "alt": 1, "shift": 2,
             "win": 3, "super": 3, "cmd": 3, "meta": 3}
    mods.sort(key=lambda m: order.get(m, 9))
    canon = {"control": "ctrl", "super": "win", "cmd": "win", "meta": "win"}
    return "+".join([canon.get(m, m) for m in mods] + rest)


def is_dangerous(combo: str) -> bool:
    return normalize(combo) in DANGEROUS


def _resolve(combo: str):
    """Split a combo into (modifier VKs, main key VK). Raises ValueError."""
    import uiautomation as auto

    parts = [p.strip().lower() for p in str(combo).split("+") if p.strip()]
    if not parts:
        raise ValueError("bo'sh kombinatsiya")

    mods, main = [], None
    for part in parts:
        if part in MODIFIERS:
            mods.append(getattr(auto.Keys, MODIFIERS[part]))
            continue
        if main is not None:
            raise ValueError(f"bir nechta asosiy tugma: {combo}")
        if part in NAMED_KEYS:
            main = getattr(auto.Keys, NAMED_KEYS[part])
        elif len(part) == 1 and (part.isalpha() or part.isdigit()):
            main = getattr(auto.Keys, f"VK_{part.upper()}")
        else:
            raise ValueError(f"noma'lum tugma: {part}")

    if main is None:
        raise ValueError("asosiy tugma ko'rsatilmagan")
    return mods, main


def _ensure_focus(handle: int) -> dict | None:
    """Put `handle` in the foreground, or refuse. Returns an error dict or None.

    This is the safety boundary for the whole module. A test typing into what
    looked like a focused Notepad found the keystrokes never arrived - Windows
    had quietly refused the foreground switch - which means blind typing goes
    to whatever the user is actually working in. So focus is verified, and if
    it cannot be confirmed nothing is sent at all.
    """
    from . import uia

    if not handle:
        return None
    if uia.foreground_handle() == handle:
        return None
    result = uia.focus_window(handle)
    if result.get("ok"):
        return None
    return {"error": result.get("error", "Oynani oldinga chiqarib bo'lmadi."),
            "sent": False}


def press(combo: str, handle: int = 0) -> dict:
    """Send one key combination to a window (focused first, and verified)."""
    if not available():
        return {"error": status()}
    blocked = _ensure_focus(handle)
    if blocked:
        return blocked

    def job():
        import time

        import uiautomation as auto

        try:
            mods, main = _resolve(combo)
        except ValueError as exc:
            return {"error": str(exc)}

        for vk in mods:
            auto.PressKey(vk)
        try:
            auto.PressKey(main)
            time.sleep(0.03)
            auto.ReleaseKey(main)
        finally:
            # Release in reverse, and always - a stuck Ctrl would wreck the
            # next thing the user types themselves.
            for vk in reversed(mods):
                auto.ReleaseKey(vk)
        return {"ok": True, "pressed": normalize(combo), "window": _focused_title()}

    return _run(job)


def type_text(text: str, handle: int = 0) -> dict:
    """Type literal text into a window (focused first, and verified)."""
    if not available():
        return {"error": status()}
    text = str(text)[:MAX_TEXT]
    if not text:
        return {"error": "matn bo'sh"}
    blocked = _ensure_focus(handle)
    if blocked:
        return blocked

    def job():
        import time

        import uiautomation as auto

        # Paste rather than synthesise keystrokes. Sending characters one by
        # one dropped and duplicated them at speed - "klaviatura testi 456"
        # arrived as "llaviatura tttti 666" - and it cannot type Cyrillic at
        # all, which this user needs. The clipboard delivers the exact string
        # in one shot, Unicode included.
        previous = ""
        try:
            previous = auto.GetClipboardText() or ""
        except Exception:
            pass

        auto.SetClipboardText(text)
        time.sleep(0.12)
        mods, main = _resolve("ctrl+v")
        for vk in mods:
            auto.PressKey(vk)
        try:
            auto.PressKey(main)
            time.sleep(0.03)
            auto.ReleaseKey(main)
        finally:
            for vk in reversed(mods):
                auto.ReleaseKey(vk)

        landed = _focused_title()
        # Put back whatever the user had copied; their clipboard is theirs.
        time.sleep(0.25)
        try:
            auto.SetClipboardText(previous)
        except Exception:
            pass
        return {"ok": True, "typed": len(text), "window": landed}

    return _run(job)


def clipboard_get() -> dict:
    if not available():
        return {"error": status()}

    def job():
        import uiautomation as auto

        value = auto.GetClipboardText() or ""
        return {"text": value[:4000], "length": len(value)}

    return _run(job)


def clipboard_set(text: str) -> dict:
    if not available():
        return {"error": status()}

    def job():
        import uiautomation as auto

        auto.SetClipboardText(str(text)[:MAX_TEXT])
        return {"ok": True, "length": len(str(text)[:MAX_TEXT])}

    return _run(job)


def _focused_title() -> str:
    """Title of the window that received the input - so the model (and the
    user) can see where it actually landed, not where it was aimed."""
    try:
        import uiautomation as auto

        from . import uia

        handle = uia.foreground_handle()
        if not handle:
            return ""
        control = auto.ControlFromHandle(handle)
        return uia._clean(control.Name)[:60] if control else ""
    except Exception:
        return ""


def _run(job) -> dict:
    """Everything Windows-facing shares the one worker thread."""
    from . import uia

    try:
        return uia._worker.call(job, timeout=30)
    except TimeoutError:
        return {"error": "Klaviatura javob bermadi."}
    except Exception as exc:
        return {"error": f"Xato: {str(exc)[:150]}"}
