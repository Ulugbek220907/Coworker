"""Windows UI Automation: the screen as structured text, not pixels.

Every Windows control already carries a name, a type, a state and a rectangle.
Serialising that costs a few hundred tokens and needs no vision model, no GPU
and no screenshot - which is the whole reason this project can drive a GUI on
a text-only model and stay free. Pixels are a fallback for later, not the
starting point.

Three things learned by measuring rather than guessing, all of which shape the
code below:

  The raw tree is unusable. A single File Explorer window is ~250 nodes of
  which most are anonymous containers, duplicated breadcrumbs, or labels whose
  "name" is a private-use glyph from an icon font. Filtering is not an
  optimisation here, it is the feature.

  Electron apps answer, but with almost nothing. VS Code reports 19 nodes for
  a whole window because Chromium keeps accessibility off until something asks
  for it. They do not hang - the documented deadlock did not reproduce - but
  they cannot be driven without --force-renderer-accessibility.

  COM is thread-affine. Every call is marshalled onto one dedicated worker
  thread with its own apartment, so control objects are never touched from the
  thread that made them.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("uia")

WALK_DEADLINE = 10.0
MAX_DEPTH = 12
MAX_ELEMENTS = 70          # what a model can usefully hold at once
CALL_TIMEOUT = 25.0

# Controls worth offering as something to act on.
INTERACTIVE = {
    "Button", "SplitButton", "Edit", "ComboBox", "CheckBox", "RadioButton",
    "MenuItem", "ListItem", "TabItem", "TreeItem", "Hyperlink", "Slider",
    "Document", "DataItem",
}
# Controls worth reading for content, but not clicking.
READABLE = {"Text", "StatusBar", "ToolTip", "Header"}

# Icon fonts (Segoe MDL2 and friends) put glyphs in the private use area and
# UIA reports them as the control's name. They are pure noise to a model.
_PUA = re.compile(r"[-\U000f0000-\U000ffffd]")
_WS = re.compile(r"\s+")

# Internal names that carry no meaning for a person or a model.
_JUNK_NAMES = {
    "chevrontextblock", "textblock", "contentpresenter", "border",
    "accessibletext", "layoutroot", "popup", "overflowbutton",
}

# An action whose label matches one of these is treated as irreversible and
# is confirmed even though ordinary clicking is not. Three languages, because
# the machine this runs on is localised in all of them.
DANGEROUS = re.compile(
    r"\b("
    r"delete|remove|erase|format|uninstall|reset|wipe|discard|"
    r"send|submit|post|publish|pay|purchase|buy|order|transfer|confirm|"
    r"shut\s*down|restart|sign\s*out|log\s*out|"
    r"удал|очист|формат|удалить|сброс|отправ|оплат|купит|перевод|подтверд|"
    r"выключ|перезагруз|выйти|"
    r"ochir|yubor|tola|sotib|tasdiq|chiqish"
    r")", re.IGNORECASE,
)


# --------------------------------------------------------------- availability

def available() -> bool:
    import importlib.util
    return bool(importlib.util.find_spec("uiautomation"))


_warmed = False


def warmup() -> None:
    """Generate the comtypes wrappers on the main thread, once, at startup.

    comtypes builds its type-library wrappers lazily and that codegen is not
    thread-safe. When a UIA call and a pycaw (audio) call first trigger it from
    different worker threads, the process segfaults - reproduced here the
    moment a window read followed a volume change under the async agent. Doing
    it up front, single-threaded, removes the lazy generation entirely so no
    two threads ever race it.
    """
    global _warmed
    if _warmed or os.name != "nt":
        return
    _warmed = True
    # Import order and COM state both matter. uiautomation must be fully
    # imported BEFORE pycaw creates any audio COM object, and - the subtle part
    # - it must be imported while COM is still UNINITIALISED on this thread.
    # Importing it after a CoInitialize() leaves a leftover pycaw endpoint to be
    # finalized during a later lazy `import uiautomation`, and that Release()
    # segfaults. So: no CoInitialize here, uiautomation first, pycaw second.
    try:
        import uiautomation  # noqa: F401
    except Exception as exc:
        log.info("warmup: uiautomation unavailable: %s", exc)
    try:
        from pycaw.pycaw import IAudioEndpointVolume, IMMDeviceEnumerator  # noqa: F401
    except Exception:
        pass  # audio is optional


def status() -> str:
    if not available():
        return "o'rnatilmagan — pip install uiautomation"
    return "tayyor"


# ------------------------------------------------------------- worker thread

class _Worker:
    """Runs every UIA call on one thread, because COM is apartment-bound.

    Touching a control object from a different thread than the one that
    created it is the classic way to get mystery E_FAIL errors, so the whole
    module funnels through here.
    """

    def __init__(self) -> None:
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, daemon=True, name="uia")
            self._thread.start()

    def _loop(self) -> None:
        import comtypes
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
        except Exception:
            try:
                comtypes.CoInitialize()
            except Exception:
                pass
        while True:
            fn, box = self._jobs.get()
            try:
                box["result"] = fn()
            except Exception as exc:              # never kill the worker
                box["error"] = exc
            finally:
                box["done"].set()

    def call(self, fn: Callable[[], Any], timeout: float = CALL_TIMEOUT) -> Any:
        self._ensure()
        box: dict[str, Any] = {"done": threading.Event()}
        self._jobs.put((fn, box))
        if not box["done"].wait(timeout):
            raise TimeoutError("UIA javob bermadi")
        if "error" in box:
            raise box["error"]
        return box.get("result")


_worker = _Worker()


# ------------------------------------------------------------------ snapshot

@dataclass
class Element:
    ref: int
    type: str
    name: str
    path: tuple[int, ...]           # child indices from the window root
    value: str = ""
    state: str = ""
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)

    def line(self) -> str:
        out = f"[{self.ref}] {self.type} \"{self.name}\""
        if self.value:
            out += f" = \"{self.value[:40]}\""
        if self.state:
            out += f" ({self.state})"
        return out


@dataclass
class Snapshot:
    title: str = ""
    handle: int = 0
    elements: list[Element] = field(default_factory=list)
    truncated: bool = False
    seconds: float = 0.0
    note: str = ""

    def by_ref(self, ref: int) -> Element | None:
        return next((e for e in self.elements if e.ref == ref), None)

    def as_text(self) -> str:
        if not self.elements:
            return "(oynada boshqariladigan element topilmadi)"
        lines = [e.line() for e in self.elements]
        if self.truncated:
            lines.append(f"... (yana elementlar bor, {MAX_ELEMENTS} tasi ko'rsatildi)")
        return "\n".join(lines)


# The most recent snapshot per window, so a click can resolve a ref later.
_snapshots: dict[int, Snapshot] = {}


# ------------------------------------------------------------------- reading

def list_windows() -> dict:
    """Visible top-level windows, newest interaction first."""
    if not available():
        return {"error": status()}

    def job():
        import uiautomation as auto

        auto.SetGlobalSearchTimeout(2)
        root = auto.GetRootControl()
        found = []
        for w in root.GetChildren():
            try:
                if w.ControlTypeName != "WindowControl":
                    continue
                name = _clean(w.Name)
                if not name:
                    continue
                r = w.BoundingRectangle
                if r.width() <= 0 or r.height() <= 0:
                    continue
                found.append({
                    "title": name[:70],
                    "class": w.ClassName,
                    "pid": w.ProcessId,
                    "handle": w.NativeWindowHandle,
                })
            except Exception:
                continue
        return found

    try:
        windows = _worker.call(job, timeout=20)
    except Exception as exc:
        return {"error": f"Oynalarni o'qib bo'lmadi: {exc}"}
    return {"windows": windows, "count": len(windows)}


def read_window(title: str = "", handle: int = 0, limit: int = MAX_ELEMENTS) -> dict:
    """Serialise one window into a numbered, actionable element list."""
    if not available():
        return {"error": status()}

    def job():
        import uiautomation as auto

        auto.SetGlobalSearchTimeout(1)
        win = _locate(auto, title, handle)
        if win is None:
            return {"error": f"Oyna topilmadi: {title or handle}"}
        return _walk(win, limit)

    try:
        result = _worker.call(job, timeout=CALL_TIMEOUT)
    except TimeoutError:
        return {"error": "Oyna javob bermadi (Electron ilova bo'lishi mumkin)."}
    except Exception as exc:
        return {"error": f"O'qishda xato: {exc}"}

    if isinstance(result, dict) and result.get("error"):
        return result

    snap: Snapshot = result
    _snapshots[snap.handle] = snap
    return {
        "window": snap.title,
        "handle": snap.handle,
        "elements": snap.as_text(),
        "count": len(snap.elements),
        "truncated": snap.truncated,
        "seconds": round(snap.seconds, 1),
        "note": snap.note,
    }


def _locate(auto, title: str, handle: int):
    root = auto.GetRootControl()
    children = root.GetChildren()
    if handle:
        for w in children:
            try:
                if w.NativeWindowHandle == handle:
                    return w
            except Exception:
                continue
        return None

    from .textutil import normalize

    wanted = normalize(title)
    best = None
    for w in children:
        try:
            if w.ControlTypeName != "WindowControl":
                continue
            name = _clean(w.Name)
            if not name:
                continue
            if name.lower() == title.lower():
                return w
            if wanted and wanted in normalize(name) and best is None:
                best = w
        except Exception:
            continue
    return best


def _walk(win, limit: int) -> Snapshot:
    started = time.monotonic()
    snap = Snapshot(title=_clean(win.Name)[:70])
    try:
        snap.handle = win.NativeWindowHandle
    except Exception:
        pass

    seen: set[tuple] = set()
    ref = 0
    # Breadth-first: the controls a person would reach for sit near the top,
    # so a truncated list still contains the useful ones.
    stack: list[tuple[Any, tuple[int, ...]]] = [(win, ())]
    while stack:
        if time.monotonic() - started > WALK_DEADLINE:
            snap.truncated = True
            snap.note = "vaqt tugadi"
            break
        node, path = stack.pop(0)
        try:
            children = node.GetChildren() if len(path) < MAX_DEPTH else []
        except Exception:
            children = []
        for i, child in enumerate(children):
            child_path = path + (i,)
            try:
                kind = child.ControlTypeName.replace("Control", "")
                name = _clean(child.Name)
                rect = child.BoundingRectangle
                on_screen = rect.width() > 0 and rect.height() > 0
            except Exception:
                continue

            if on_screen:
                stack.append((child, child_path))

            if not name or not on_screen:
                continue
            if name.lower() in _JUNK_NAMES:
                continue
            if kind not in INTERACTIVE and kind not in READABLE:
                continue

            key = (kind, name.lower())
            if key in seen:                       # breadcrumbs repeat a lot
                continue
            seen.add(key)

            if len(snap.elements) >= limit:
                snap.truncated = True
                continue

            ref += 1
            snap.elements.append(Element(
                ref=ref, type=kind, name=name[:60], path=child_path,
                value=_value_of(child, kind), state=_state_of(child),
                rect=(rect.left, rect.top, rect.right, rect.bottom),
            ))

    snap.seconds = time.monotonic() - started
    if not snap.elements:
        web = _web_alternative(snap.title)
        if web:
            snap.note = (
                f"Bu ilovaning tugmalarini o'qib bo'lmadi (Telegram/Qt kabi "
                f"ilovalar accessibility bermaydi). ISHONCHLI YO'L: brauzerda "
                f"web versiyasini och — `web_open` bilan {web} — u yerda "
                f"akkauntingiz bilan bemalol bosish/yozish mumkin."
            )
        else:
            snap.note = (
                "Bo'sh daraxt. Electron/Qt ilovasi accessibility bermayapti. "
                "Web versiyasi bo'lsa, brauzerda ochib boshqargan ma'qul."
            )
    return snap


# Native apps whose UIA tree is unusable but which have a full web version the
# browser layer drives reliably. Routing "control Telegram" through
# web.telegram.org sidesteps the accessibility problem entirely.
_WEB_APPS = {
    "telegram": "https://web.telegram.org/a/",
    "discord": "https://discord.com/app",
    "whatsapp": "https://web.whatsapp.com",
    "slack": "https://app.slack.com",
    "spotify": "https://open.spotify.com",
}


def _web_alternative(title: str) -> str:
    low = (title or "").lower()
    for name, url in _WEB_APPS.items():
        if name in low:
            return url
    return ""


def web_alternative(app_name: str) -> str:
    """Public: the web URL for an app the browser can drive instead."""
    return _web_alternative(app_name)


def _value_of(control, kind: str) -> str:
    if kind not in ("Edit", "ComboBox", "Document", "Slider"):
        return ""
    try:
        pattern = control.GetValuePattern()
        return _clean(pattern.Value)[:60] if pattern else ""
    except Exception:
        return ""


def _state_of(control) -> str:
    bits = []
    try:
        if not control.IsEnabled:
            bits.append("o'chiq")
    except Exception:
        pass
    try:
        if control.HasKeyboardFocus:
            bits.append("fokusda")
    except Exception:
        pass
    try:
        toggle = control.GetTogglePattern()
        if toggle is not None:
            bits.append("belgilangan" if toggle.ToggleState == 1 else "belgilanmagan")
    except Exception:
        pass
    return ", ".join(bits)


def _clean(name: Any) -> str:
    if not name:
        return ""
    text = _PUA.sub("", str(name))
    return _WS.sub(" ", text).strip()


# ------------------------------------------------------------------- acting

def is_dangerous(label: str) -> bool:
    return bool(DANGEROUS.search(label or ""))


def click(handle: int, ref: int) -> dict:
    """Activate an element through its UIA pattern, falling back to a click.

    The pattern route is preferred because it works on a background window and
    does not move the user's mouse or steal their focus - exactly the problem
    Microsoft's UFO research solved with a picture-in-picture desktop.
    """
    return _act(handle, ref, _do_click)


def set_text(handle: int, ref: int, text: str) -> dict:
    return _act(handle, ref, lambda c, e: _do_type(c, e, text))


def _act(handle: int, ref: int, action) -> dict:
    if not available():
        return {"error": status()}
    snap = _snapshots.get(handle)
    if snap is None:
        return {"error": "Avval read_window bilan oynani o'qing."}
    element = snap.by_ref(ref)
    if element is None:
        return {"error": f"[{ref}] elementi bu oynada yo'q."}

    def job():
        import uiautomation as auto

        auto.SetGlobalSearchTimeout(1)
        win = _locate(auto, "", handle)
        if win is None:
            return {"error": "Oyna yopilgan."}
        control = _resolve(win, element)
        if control is None:
            return {"error": f"«{element.name}» endi topilmadi — oyna o'zgargan. Qayta o'qing."}
        return action(control, element)

    try:
        return _worker.call(job, timeout=CALL_TIMEOUT)
    except TimeoutError:
        return {"error": "Amal javob bermadi."}
    except Exception as exc:
        return {"error": f"Xato: {exc}"}


def set_window_state(handle: int, state: int) -> dict:
    """Maximize / minimize / restore a window by handle.

    Uses WindowPattern.SetWindowVisualState, which was verified to move a real
    window between states and back. No mouse, no title-bar hunting.
    """
    if not available():
        return {"error": status()}

    def job():
        import uiautomation as auto

        auto.SetGlobalSearchTimeout(1)
        win = _locate(auto, "", handle)
        if win is None:
            return {"error": "Oyna topilmadi."}
        try:
            win.GetWindowPattern().SetWindowVisualState(state)
            return {"ok": True, "window": _clean(win.Name)[:60], "state": state}
        except Exception as exc:
            return {"error": f"Oyna holatini o'zgartirib bo'lmadi: {exc}"}

    return _window_call(job)


def foreground_handle() -> int:
    """Whichever window actually has the keyboard right now."""
    if os.name != "nt":
        return 0
    try:
        import ctypes

        return int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return 0


def _force_foreground(handle: int) -> bool:
    """Genuinely move the OS foreground to `handle`, and report whether it moved.

    Windows refuses SetForegroundWindow from a process that is not already in
    the foreground, so a plain call silently does nothing - which is how
    keystrokes end up in whatever the user was typing in. Attaching to the
    current foreground thread's input queue lifts that restriction. The return
    value is checked against GetForegroundWindow rather than trusted.
    """
    if os.name != "nt":
        return False
    import ctypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    SW_RESTORE = 9

    try:
        if user32.IsIconic(handle):
            user32.ShowWindow(handle, SW_RESTORE)

        target_thread = user32.GetWindowThreadProcessId(handle, None)
        current_thread = kernel32.GetCurrentThreadId()
        fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)

        attached = []
        for other in {target_thread, fg_thread}:
            if other and other != current_thread and user32.AttachThreadInput(current_thread, other, True):
                attached.append(other)
        try:
            user32.BringWindowToTop(handle)
            user32.SetForegroundWindow(handle)
            user32.SetActiveWindow(handle)
        finally:
            for other in attached:
                user32.AttachThreadInput(current_thread, other, False)
    except Exception as exc:
        log.info("foreground failed: %s", exc)
        return False

    import time

    for _ in range(10):                 # give the switch a moment to settle
        if foreground_handle() == handle:
            return True
        time.sleep(0.05)
    return foreground_handle() == handle


def click_at(x: int, y: int) -> dict:
    """Real mouse click at screen coordinates.

    Electron/Chromium editors (Antigravity, VS Code, Cursor) do not accept
    keyboard input reliably until a real click gives their internal editor the
    caret - window focus alone is not enough. Clicking a large target like a
    chat input box does not need pixel precision, so a coarse vision-located
    point is enough; a terminal is happy with a click anywhere too.
    """
    if not available():
        return {"error": status()}

    def job():
        import uiautomation as auto

        try:
            auto.Click(int(x), int(y), waitTime=0.1)
            return {"ok": True, "at": [int(x), int(y)]}
        except Exception as exc:
            return {"error": f"Bosib bo'lmadi: {exc}"}

    return _window_call(job)


def focus_window(handle: int) -> dict:
    """Bring a window to the front, and confirm it actually came forward."""
    if not available():
        return {"error": status()}

    def job():
        import uiautomation as auto

        auto.SetGlobalSearchTimeout(1)
        win = _locate(auto, "", handle)
        if win is None:
            return {"error": "Oyna topilmadi."}
        name = _clean(win.Name)[:60]
        try:
            if win.GetWindowPattern().WindowVisualState == 2:
                win.GetWindowPattern().SetWindowVisualState(0)
        except Exception:
            pass
        try:
            win.SetFocus()
        except Exception:
            pass

        if _force_foreground(handle):
            return {"ok": True, "window": name, "foreground": True}
        return {
            "ok": False,
            "window": name,
            "foreground": False,
            "error": (
                f"«{name}» oynasini oldinga chiqarib bo'lmadi. Windows ba'zan "
                "buni to'sadi (to'liq ekrandagi ilova yoki administrator oynasi)."
            ),
        }

    return _window_call(job)


def close_window(handle: int) -> dict:
    """Close a window. Irreversible - the caller must confirm first."""
    if not available():
        return {"error": status()}

    def job():
        import uiautomation as auto

        auto.SetGlobalSearchTimeout(1)
        win = _locate(auto, "", handle)
        if win is None:
            return {"error": "Oyna topilmadi (yopilgan bo'lishi mumkin)."}
        name = _clean(win.Name)[:60]
        try:
            win.GetWindowPattern().Close()
            return {"ok": True, "closed": name}
        except Exception as exc:
            return {"error": f"Oynani yopib bo'lmadi: {exc}"}

    return _window_call(job)


def window_by_handle(handle: int) -> dict | None:
    """Look up a window's title/state for the confirmation prompt."""
    def job():
        import uiautomation as auto

        auto.SetGlobalSearchTimeout(1)
        win = _locate(auto, "", handle)
        if win is None:
            return None
        try:
            return {"title": _clean(win.Name)[:70],
                    "state": win.GetWindowPattern().WindowVisualState}
        except Exception:
            return {"title": _clean(win.Name)[:70], "state": 0}

    try:
        return _worker.call(job, timeout=15)
    except Exception:
        return None


def _window_call(job) -> dict:
    try:
        return _worker.call(job, timeout=CALL_TIMEOUT)
    except TimeoutError:
        return {"error": "Oyna javob bermadi."}
    except Exception as exc:
        return {"error": f"Xato: {exc}"}


def _resolve(win, element: Element):
    """Re-find the control, verifying it is still what we snapshotted.

    A stored COM pointer would be stale the moment the window repaints, so the
    path is re-walked and the identity re-checked. If the layout shifted, fall
    back to finding the same type and name anywhere in the window - and if
    that fails too, refuse rather than click whatever now sits at that index.
    """
    node = win
    for index in element.path:
        try:
            children = node.GetChildren()
        except Exception:
            return None
        if index >= len(children):
            node = None
            break
        node = children[index]

    if node is not None:
        try:
            if (node.ControlTypeName.replace("Control", "") == element.type
                    and _clean(node.Name)[:60] == element.name):
                return node
        except Exception:
            pass

    # Layout moved: search by identity instead of position.
    stack = [win]
    checked = 0
    while stack and checked < 600:
        current = stack.pop(0)
        checked += 1
        try:
            if (current.ControlTypeName.replace("Control", "") == element.type
                    and _clean(current.Name)[:60] == element.name):
                return current
            stack.extend(current.GetChildren())
        except Exception:
            continue
    return None


def _do_click(control, element: Element) -> dict:
    for attempt in (
        lambda: control.GetInvokePattern().Invoke(),
        lambda: control.GetTogglePattern().Toggle(),
        lambda: control.GetSelectionItemPattern().Select(),
        lambda: control.GetExpandCollapsePattern().Expand(),
    ):
        try:
            attempt()
            return {"ok": True, "clicked": element.name, "via": "UIA pattern"}
        except Exception:
            continue
    # Last resort: a real mouse click, which does take over the pointer.
    try:
        control.Click(simulateMove=False)
        return {"ok": True, "clicked": element.name, "via": "sichqoncha"}
    except Exception as exc:
        return {"error": f"Bosib bo'lmadi: {exc}"}


def _do_type(control, element: Element, text: str) -> dict:
    try:
        control.GetValuePattern().SetValue(text)
        return {"ok": True, "typed_into": element.name, "via": "UIA pattern"}
    except Exception:
        pass
    try:
        control.SetFocus()
        control.SendKeys("{Ctrl}a", waitTime=0.05)
        control.SendKeys(_escape_keys(text), waitTime=0.02)
        return {"ok": True, "typed_into": element.name, "via": "klaviatura"}
    except Exception as exc:
        return {"error": f"Yozib bo'lmadi: {exc}"}


def _escape_keys(text: str) -> str:
    """uiautomation treats {} and friends as key names."""
    return text.replace("{", "{{}").replace("}", "{}}")
