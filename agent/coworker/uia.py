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

Three rules the tool layer depends on:

  Names match exactly. A window or control is chosen by its full cleaned name,
  case and apostrophes folded, never by a substring: "Notes" must not reach
  "Notes Pro". When several windows share a name the call refuses and returns
  the candidates with their handles, so the model can pick one by handle.

  An action that outlives its timeout is abandoned. Its result is dropped and
  the outcome is reported as unknown. A UI that stops answering usually keeps
  not answering, so after three abandoned jobs the worker is switched off until
  the process restarts.

  Blocked windows are never read. A password manager's tree is refused by title
  or class, the same list the screenshot path uses.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .core.types import normalize_text

log = logging.getLogger("uia")

WALK_DEADLINE = 10.0
MAX_DEPTH = 12
MAX_ELEMENTS = 70          # what a model can usefully hold at once
CALL_TIMEOUT = 25.0
MAX_ABANDONED = 3          # section 7: three abandoned jobs disable the pool

# Controls worth offering as something to act on.
INTERACTIVE = {
    "Button", "SplitButton", "Edit", "ComboBox", "CheckBox", "RadioButton",
    "MenuItem", "ListItem", "TabItem", "TreeItem", "Hyperlink", "Slider",
    "Document", "DataItem",
}
# Controls worth reading for content, but not clicking.
READABLE = {"Text", "StatusBar", "ToolTip", "Header"}

# Icon fonts (Segoe MDL2 and friends) put glyphs in the private use areas and
# UIA reports them as the control's name. Only those glyphs are removed: a
# hyphen is part of a real name ("Save-As") and must stay.
_PUA = re.compile(r"[-\U000f0000-\U000ffffd\U00100000-\U0010fffd]")
_WS = re.compile(r"\s+")

# Internal names that carry no meaning for a person or a model.
_JUNK_NAMES = {
    "chevrontextblock", "textblock", "contentpresenter", "border",
    "accessibletext", "layoutroot", "popup", "overflowbutton",
}

# Windows whose contents are never read or captured, matched by title or class.
# Password managers and the Windows credential and security prompts. The owner
# may add more under "blocked_windows" in the config.
DEFAULT_BLOCKED_WINDOWS: tuple[str, ...] = (
    "keepass", "bitwarden", "1password", "lastpass", "dashlane", "keeper", "enpass",
    "password", "parol", "credential", "windows security", "windows hello",
)
BLOCKED_TEXT = "Bu oyna maxfiy deb belgilangan — o'qilmaydi va skrinshoti olinmaydi."


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

class JobAbandoned(TimeoutError):
    """A UIA job outlived its timeout. Its result is dropped and the outcome is unknown."""


class PoolDisabled(RuntimeError):
    """Too many jobs were abandoned. The UIA pool stays off until the process restarts."""


def _init_com() -> None:
    """Start COM on the UIA thread. Off Windows there is no COM, so nothing to do."""
    if os.name != "nt":
        return
    import comtypes
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
    except Exception:
        try:
            comtypes.CoInitialize()
        except Exception:
            pass


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
        self._abandoned = 0

    def _ensure(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, daemon=True, name="uia")
            self._thread.start()

    def _loop(self) -> None:
        _init_com()
        while True:
            fn, box = self._jobs.get()
            try:
                box["result"] = fn()
            except Exception as exc:              # never kill the worker
                box["error"] = exc
            finally:
                box["done"].set()

    def call(self, fn: Callable[[], Any], timeout: float = CALL_TIMEOUT) -> Any:
        with self._lock:
            if self._abandoned >= MAX_ABANDONED:
                raise PoolDisabled(
                    "UIA xizmati o'chirilgan: ketma-ket uchta amal osilib qoldi. "
                    "Ilovani qayta ishga tushiring."
                )
        self._ensure()
        box: dict[str, Any] = {"done": threading.Event()}
        self._jobs.put((fn, box))
        if not box["done"].wait(timeout):
            # The job keeps running on the worker, but nobody waits for it any
            # more: its result is dropped and the caller learns the outcome is unknown.
            with self._lock:
                self._abandoned += 1
                count = self._abandoned
            log.warning("UIA job abandoned after %.0fs (%d of %d)", timeout, count, MAX_ABANDONED)
            raise JobAbandoned("UIA javob bermadi")
        if "error" in box:
            raise box["error"]
        return box.get("result")


_worker = _Worker()


def run(fn: Callable[[], Any], timeout: float = CALL_TIMEOUT) -> Any:
    """Run `fn` on the UIA thread. Failures come back as {"error", "code"} dicts."""
    try:
        return _worker.call(fn, timeout=timeout)
    except JobAbandoned:
        return {
            "error": "Amal javob bermadi — natija noma'lum. Oynani qayta o'qing.",
            "code": "timeout", "outcome": "unknown",
        }
    except PoolDisabled as exc:
        return {"error": str(exc), "code": "throttled"}
    except Exception as exc:
        return {"error": f"Xato: {str(exc)[:150]}"}


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
    is_password: bool = False       # the value of a password field is never read

    def line(self) -> str:
        out = f"[{self.ref}] {self.type} \"{self.name}\""
        if self.is_password:
            out += " (parol: qiymati ko'rsatilmaydi)"
        elif self.value:
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


def snapshot(handle: int) -> Snapshot | None:
    """The most recent read of a window. Refs in actions refer to this snapshot."""
    return _snapshots.get(handle)


# ------------------------------------------------------------ pure selection

def is_blocked_window(title: str, class_name: str, patterns: Iterable[str]) -> bool:
    """True when the title or the class of a window contains a blocked pattern.

    Substring and case-folded on purpose: a password manager shows its name in
    many forms ("KeePass", "KeePassXC - Database") and a near miss is a leak.
    """
    fields = (normalize_text(title), normalize_text(class_name))
    for pattern in patterns:
        needle = normalize_text(str(pattern))
        if needle and any(needle in f for f in fields):
            return True
    return False


def pick_window(names: list[str], wanted: str) -> tuple[int | None, list[int]]:
    """The index of the one window named `wanted`, or the indexes of the tie.

    Returns (index, []) for a unique exact match, (None, indexes) when several
    windows carry that name, and (None, []) when none does.
    """
    key = normalize_text(wanted)
    if not key:
        return None, []
    hits = [i for i, name in enumerate(names) if normalize_text(name) == key]
    if len(hits) == 1:
        return hits[0], []
    return None, hits


def similar_titles(names: list[str], wanted: str, limit: int = 8) -> list[str]:
    """Titles that merely contain `wanted`. Shown as hints, never acted on."""
    key = normalize_text(wanted)
    if not key:
        return []
    return [name for name in names if key in normalize_text(name)][:limit]


# ------------------------------------------------------------------- reading

def list_windows() -> dict:
    """Visible top-level windows with their handles."""
    if not available():
        return {"error": status()}

    def job():
        import uiautomation as auto

        auto.SetGlobalSearchTimeout(2)
        found = []
        for w in auto.GetRootControl().GetChildren():
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
                    "title": name,
                    "class": w.ClassName,
                    "pid": w.ProcessId,
                    "handle": w.NativeWindowHandle,
                })
            except Exception:
                continue
        return found

    windows = run(job, timeout=20)
    if isinstance(windows, dict):
        return windows
    return {"windows": windows, "count": len(windows)}


def find_window(title: str) -> dict:
    """The handle and exact title of the one window called `title`.

    Returns {"handle", "title"}, or an error. A tie comes back with the
    candidate windows so the caller can choose one by handle.
    """
    if not available():
        return {"error": status()}

    def job():
        import uiautomation as auto

        auto.SetGlobalSearchTimeout(1)
        control, err = _select(auto, title, 0)
        if err:
            return err
        handle = _handle_of(control)
        if not handle:
            # Handle 0 would mean "the whole screen" to a screenshot, so it is never returned.
            return {"error": "Oynaning handle'i aniqlanmadi.", "code": "element_gone"}
        return {"handle": handle, "title": _clean(control.Name), "pid": _pid_of(control)}

    return run(job, timeout=8)


def _pid_of(control) -> int:
    try:
        return int(control.ProcessId)
    except Exception:
        return 0


def process_image(pid: int) -> str:
    """The lower-cased file name of a process's program (for example "mintty.exe"), or "".

    The window title is chosen by the program it shows, so the program is the
    stronger signal for "this window runs commands". Returns "" when the process
    cannot be read; callers treat that as unknown, never as harmless.
    """
    if os.name != "nt" or not pid:
        return ""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(1024)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return os.path.basename(buffer.value).lower()
    except Exception:
        return ""
    finally:
        kernel32.CloseHandle(handle)


def read_window(
    title: str = "",
    handle: int = 0,
    limit: int = MAX_ELEMENTS,
    blocked: Iterable[str] = DEFAULT_BLOCKED_WINDOWS,
) -> dict:
    """Serialise one window into a numbered, actionable element list."""
    if not available():
        return {"error": status()}
    patterns = tuple(blocked)

    def job():
        import uiautomation as auto

        auto.SetGlobalSearchTimeout(1)
        win, err = _select(auto, title, handle)
        if err:
            return err
        if is_blocked_window(_clean(win.Name), _class_of(win), patterns):
            return {"error": BLOCKED_TEXT, "code": "blocked_window"}
        return _walk(win, limit)

    result = run(job)
    if isinstance(result, dict):
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


def _select(auto, title: str, handle: int) -> tuple[Any, dict | None]:
    """Choose one top-level window. Runs on the UIA thread.

    Returns (control, None), or (None, error) with the error dict to hand back.
    A handle wins over a title. A title must match one window exactly.
    """
    children = auto.GetRootControl().GetChildren()
    if handle:
        for w in children:
            try:
                if w.NativeWindowHandle == handle:
                    return w, None
            except Exception:
                continue
        return None, {"error": "Oyna topilmadi (yopilgan bo'lishi mumkin).", "code": "element_gone"}

    wanted = _clean(title)
    if not wanted:
        return None, {"error": "Oyna nomi yoki handle kerak.", "code": "arg_invalid"}
    controls: list[Any] = []
    names: list[str] = []
    for w in children:
        try:
            if w.ControlTypeName != "WindowControl":
                continue
            name = _clean(w.Name)
            if not name:
                continue
            controls.append(w)
            names.append(name)
        except Exception:
            continue

    index, tied = pick_window(names, wanted)
    if index is not None:
        return controls[index], None
    if tied:
        return None, {
            "error": f"«{wanted}» nomli bir nechta oyna bor. handle bilan aniqlang.",
            "code": "ambiguous_window",
            "candidates": [{"title": names[i], "handle": _handle_of(controls[i])} for i in tied],
        }
    return None, {
        "error": f"Oyna topilmadi: «{wanted}». Ro'yxatdagi aniq nomini yoki handle'ini ishlating.",
        "similar": similar_titles(names, wanted),
    }


def _handle_of(control) -> int:
    try:
        return int(control.NativeWindowHandle)
    except Exception:
        return 0


def _class_of(control) -> str:
    try:
        return str(control.ClassName or "")
    except Exception:
        return ""


def _walk(win, limit: int) -> Snapshot:
    started = time.monotonic()
    snap = Snapshot(title=_clean(win.Name))
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
            password = _is_password(child, kind)
            snap.elements.append(Element(
                ref=ref, type=kind, name=name[:60], path=child_path,
                value="" if password else _value_of(child, kind),
                state=_state_of(child),
                rect=(rect.left, rect.top, rect.right, rect.bottom),
                is_password=password,
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


def _is_password(control, kind: str) -> bool:
    if kind != "Edit":
        return False
    try:
        return bool(control.IsPassword)
    except Exception:
        return False


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

def click(
    handle: int,
    ref: int,
    blocked: Iterable[str] = DEFAULT_BLOCKED_WINDOWS,
    expect_name: str | None = None,
) -> dict:
    """Activate an element through its UIA pattern, falling back to a checked pointer click.

    The pattern route is preferred because it works on a background window and
    does not move the user's mouse or steal their focus - exactly the problem
    Microsoft's UFO research solved with a picture-in-picture desktop.

    ``expect_name`` is the label the owner was shown on the confirmation card. A
    ref is only a position in the newest snapshot, so the click is refused when
    that position now holds a control with another label; the card's risk
    classification applies to the label, and a different label would escape it.
    """
    element, err = _cached(handle, ref)
    if err:
        return err
    if expect_name is not None and normalize_text(element.name) != normalize_text(expect_name):
        return {
            "error": f"[{ref}] endi «{element.name}» — tasdiqlangan «{expect_name}» emas. Oynani qayta o'qing.",
            "code": "element_gone",
        }
    return _act(handle, element, lambda c, e: _do_click(c, e, handle), tuple(blocked))


def set_text(handle: int, ref: int, text: str, blocked: Iterable[str] = DEFAULT_BLOCKED_WINDOWS) -> dict:
    """Put text into an editable element. Password fields are refused."""
    element, err = _cached(handle, ref)
    if err:
        return err
    if element.is_password:
        return {"error": "Parol maydoniga matn yozib bo'lmaydi.", "code": "password_field"}
    return _act(handle, element, lambda c, e: _do_type(c, e, text, handle), tuple(blocked))


def _cached(handle: int, ref: int) -> tuple[Element | None, dict | None]:
    if not available():
        return None, {"error": status()}
    snap = _snapshots.get(handle)
    if snap is None:
        return None, {"error": "Avval read_window bilan oynani o'qing.", "code": "element_gone"}
    element = snap.by_ref(ref)
    if element is None:
        return None, {"error": f"[{ref}] elementi bu oynada yo'q.", "code": "element_gone"}
    return element, None


def _act(handle: int, element: Element, action, blocked: tuple[str, ...]) -> dict:
    def job():
        import uiautomation as auto

        auto.SetGlobalSearchTimeout(1)
        win, err = _select(auto, "", handle)
        if err:
            return err
        if is_blocked_window(_clean(win.Name), _class_of(win), blocked):
            return {"error": BLOCKED_TEXT, "code": "blocked_window"}
        control = _resolve(win, element)
        if control is None:
            return {
                "error": f"«{element.name}» endi topilmadi — oyna o'zgargan. Qayta o'qing.",
                "code": "element_gone",
            }
        return action(control, element)

    return run(job)


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
        win, err = _select(auto, "", handle)
        if err:
            return err
        try:
            win.GetWindowPattern().SetWindowVisualState(state)
            return {"ok": True, "window": _clean(win.Name)[:60], "state": state}
        except Exception as exc:
            return {"error": f"Oyna holatini o'zgartirib bo'lmadi: {exc}"}

    return run(job)


def foreground_handle() -> int:
    """Whichever window actually has the keyboard right now."""
    if os.name != "nt":
        return 0
    try:
        import ctypes

        return int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return 0


def ensure_foreground(handle: int, blocked: Iterable[str] = DEFAULT_BLOCKED_WINDOWS) -> dict | None:
    """Make `handle` the foreground window. Returns None once it is, or an error with sent=False.

    A blocked window is refused even when it already has the focus: keys typed
    into a password manager are as much a leak as keys read out of one.

    Call this only inside a UIA job (it runs on the worker thread). A caller
    that sends keys calls it in the same job as the keystrokes, so no other job
    can take the focus between the check and the send.
    """
    if not handle:
        return {"error": "Oyna tanlanmagan (handle 0).", "code": "arg_invalid", "sent": False}
    import uiautomation as auto

    auto.SetGlobalSearchTimeout(1)
    win, err = _select(auto, "", handle)
    if err:
        return {**err, "sent": False}
    if is_blocked_window(_clean(win.Name), _class_of(win), tuple(blocked)):
        return {"error": BLOCKED_TEXT, "code": "blocked_window", "sent": False}
    if foreground_handle() == handle:
        return None
    result = _bring_forward(win, handle)
    if result.get("ok"):
        return None
    return {**result, "sent": False}


def ensure_not_password_focus(auto: Any) -> dict | None:
    """Refuse when the control that would receive keystrokes is a password field.

    A window can be the foreground one and still have a password box focused (a
    login page in a browser), so the window check alone does not stop text from
    landing in a secret. When the focused control cannot be read at all, nothing is
    sent either: the owner cannot be sure where the keys would go.

    Call this inside the same UIA job as the keystrokes, after ensure_foreground.
    """
    try:
        focused = auto.GetFocusedControl()
    except Exception:
        return {"error": "Fokusdagi maydonni aniqlab bo'lmadi — tugmalar yuborilmadi.",
                "code": "focus_unknown", "sent": False}
    try:
        is_password = bool(getattr(focused, "IsPassword", False)) if focused is not None else False
    except Exception:
        return {"error": "Fokusdagi maydonni aniqlab bo'lmadi — tugmalar yuborilmadi.",
                "code": "focus_unknown", "sent": False}
    if is_password:
        return {"error": "Fokus parol maydonida — matn yuborilmadi.", "code": "password_field", "sent": False}
    return None


def _bring_forward(win, handle: int) -> dict:
    """Bring a window to the front and confirm it came forward. Runs on the UIA thread."""
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

    for _ in range(10):                 # give the switch a moment to settle
        if foreground_handle() == handle:
            return True
        time.sleep(0.05)
    return foreground_handle() == handle


def focus_window(handle: int) -> dict:
    """Bring a window to the front, and confirm it actually came forward."""
    if not available():
        return {"error": status()}

    def job():
        import uiautomation as auto

        auto.SetGlobalSearchTimeout(1)
        win, err = _select(auto, "", handle)
        if err:
            return err
        return _bring_forward(win, handle)

    return run(job)


def close_window(handle: int) -> dict:
    """Ask a window to close. Irreversible once it happens - the caller must confirm first.

    A window may refuse: an unsaved-changes prompt is a normal answer. So the
    result is close_requested, never closed, and it is never reported as done.
    """
    if not available():
        return {"error": status()}

    def job():
        import uiautomation as auto

        auto.SetGlobalSearchTimeout(1)
        win, err = _select(auto, "", handle)
        if err:
            return err
        name = _clean(win.Name)[:60]
        try:
            win.GetWindowPattern().Close()
            return {"ok": True, "close_requested": name}
        except Exception as exc:
            return {"error": f"Oynani yopish so'rovini yuborib bo'lmadi: {exc}"}

    return run(job)


def window_by_handle(handle: int) -> dict | None:
    """Look up a window's title and state for a confirmation prompt."""
    def job():
        import uiautomation as auto

        auto.SetGlobalSearchTimeout(1)
        win, err = _select(auto, "", handle)
        if err:
            return None
        try:
            return {"title": _clean(win.Name)[:70],
                    "state": win.GetWindowPattern().WindowVisualState}
        except Exception:
            return {"title": _clean(win.Name)[:70], "state": 0}

    info = run(job, timeout=15)
    return info if isinstance(info, dict) and "title" in info else None


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


def _do_click(control, element: Element, handle: int) -> dict:
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

    # Last resort: a real pointer click. The pointer goes wherever it is, so the
    # click is made only when the target window is in front and the control's
    # centre really belongs to it - otherwise another window would take it.
    import uiautomation as auto

    err = ensure_foreground(handle)
    if err:
        return err
    rect = control.BoundingRectangle
    hit = auto.ControlFromPoint((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
    if hit is None or _handle_of(hit.GetTopLevelControl()) != handle:
        return {
            "error": f"«{element.name}» boshqa oyna ostida qolgan — sichqoncha bilan bosilmadi.",
            "code": "element_gone",
        }
    try:
        control.Click(simulateMove=False)
        return {"ok": True, "clicked": element.name, "via": "sichqoncha"}
    except Exception as exc:
        return {"error": f"Bosib bo'lmadi: {exc}"}


def _do_type(control, element: Element, text: str, handle: int) -> dict:
    try:
        control.GetValuePattern().SetValue(text)
        return {"ok": True, "typed_into": element.name, "via": "UIA pattern"}
    except Exception:
        pass
    # Keystrokes reach the focused window only, so they are sent after the target
    # has been focused and verified in this same job.
    err = ensure_foreground(handle)
    if err:
        return err
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
