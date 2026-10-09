"""Reading a screen with a vision model - the phase-3 fallback.

Scope was set by measurement, not ambition. DeepSeek's vision model
(deepseek-flash) describes a screen accurately - "dark background, blue Login
button" - so it is genuinely useful for the screens the UIA layer returns
empty for: Electron apps, canvas UIs, games, embedded viewers. But its pixel
*coordinate* grounding is unreliable and token-expensive, exactly as the
research warned, so this module deliberately does READING only. It does not
click by pixel; that would demo once and then fail.

Capture is local and never changes what the owner is doing:

  A single window is captured by its handle with PrintWindow. The window renders
  its own content even when another window covers it, so nothing is brought to
  the front, focused or restored.

  The whole screen is captured with ImageGrab, and every visible blocked window
  (a password manager, a credential prompt) is painted over before the image
  leaves this module. A blocked window is never sent, even inside a full view.

  DPI awareness is set once per process. Without it, window rectangles and
  grabbed pixels disagree on scaled displays and a window is cropped or offset.

Only the resulting screenshot is sent to the model, and only when the owner
asked to read a screen - never continuously.
"""
from __future__ import annotations

import base64
import ctypes
import io
import logging
import os
from dataclasses import dataclass
from typing import Iterable

from . import uia

log = logging.getLogger("vision")

# DeepSeek downscales images to ~1024 tokens anyway; keep the longest side
# modest so the upload is small and the cost stays near zero.
MAX_SIDE = 1400
JPEG_QUALITY = 80
PW_RENDERFULLCONTENT = 0x00000002

DEFAULT_PROMPT = (
    "Bu kompyuter ekranining rasmi. Foydalanuvchi savoliga QISQA javob ber. "
    "Ekranda nima ko'rinayotganini ayt: matn, tugmalar, xatolik xabari, holat. "
    "Ko'rmagan narsangni o'ylab topma."
)
APP_PROMPT = (
    "Bu ilova oynasining rasmi. Oynada ko'rinib turgan matnni o'qib ber: oxirgi "
    "xabarlar, javoblar yoki natijalar yuqoridan pastga. Qisqa yoz. "
    "Ko'rinmayotgan narsani o'ylab topma."
)

_dpi_done = False
_bound = False


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str
    class_name: str
    rect: tuple[int, int, int, int]     # left, top, right, bottom in physical pixels
    minimized: bool


def available() -> bool:
    import importlib.util
    return os.name == "nt" and bool(importlib.util.find_spec("PIL"))


def status() -> str:
    import importlib.util
    if os.name != "nt":
        return "faqat Windows'da ishlaydi"
    if not importlib.util.find_spec("PIL"):
        return "o'rnatilmagan — pip install pillow"
    return "tayyor"


def ensure_dpi_aware() -> None:
    """Make this process DPI aware, once. Later calls do nothing.

    Windows only honours this before the process creates its first window, and
    repeating it is noise, so the first call decides for the whole process.
    """
    global _dpi_done
    if _dpi_done:
        return
    _dpi_done = True
    _set_dpi_awareness()


def _set_dpi_awareness() -> None:
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):   # per-monitor v2
            return
    except AttributeError:
        pass                                                           # very old Windows
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)                 # per-monitor
    except (AttributeError, OSError):
        try:
            user32.SetProcessDPIAware()
        except AttributeError:
            log.info("DPI awareness could not be set")


def capture(handle: int = 0, *, blocked: Iterable[str] = uia.DEFAULT_BLOCKED_WINDOWS) -> dict:
    """A JPEG of one window (by handle) or of the primary screen.

    Returns {"image_b64", "size", "scope"}, or {"error", "code"}. Never moves
    focus and never changes a window's state.
    """
    if not available():
        return {"error": status()}
    ensure_dpi_aware()
    patterns = tuple(blocked)
    try:
        if handle:
            grabbed = _grab_window(handle, patterns)
            if isinstance(grabbed, dict):
                return grabbed
            image, scope = grabbed
        else:
            image, scope = _grab_screen(patterns), "butun ekran"
        return _encode(image, scope)
    except Exception as exc:                 # a Win32 failure is a refused capture, not a crash
        log.warning("capture failed: %s", type(exc).__name__)
        return {"error": f"Skrinshot olinmadi: {str(exc)[:120]}"}


def fit_size(width: int, height: int, max_side: int = MAX_SIDE) -> tuple[int, int]:
    """The size an image is sent at: its longest side brought down to max_side."""
    longest = max(width, height)
    if longest <= max_side:
        return width, height
    scale = max_side / longest
    return max(1, int(width * scale)), max(1, int(height * scale))


def mask_rects(windows: Iterable[WindowInfo], patterns: Iterable[str]) -> list[tuple[int, int, int, int]]:
    """The rectangles of the windows whose title or class is blocked."""
    patterns = tuple(patterns)
    return [w.rect for w in windows if uia.is_blocked_window(w.title, w.class_name, patterns)]


def _grab_window(handle: int, patterns: tuple[str, ...]):
    info = _window_info(handle)
    if info is None:
        return {"error": "Oyna topilmadi."}
    if uia.is_blocked_window(info.title, info.class_name, patterns):
        return {"error": uia.BLOCKED_TEXT, "code": "blocked_window"}
    if info.minimized:
        return {"error": "Oyna yig'ilgan — skrinshot olinmaydi. Avval uni oching."}
    left, top, right, bottom = info.rect
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return {"error": "Oynaning o'lchami noma'lum."}
    image = _print_window(handle, width, height)
    if image is None:
        return {"error": "Oynaning skrinshotini olib bo'lmadi."}
    return image, info.title or "oyna"


def _grab_screen(patterns: tuple[str, ...]):
    from PIL import ImageDraw

    image = _grab_primary().convert("RGB")
    draw = ImageDraw.Draw(image)
    for left, top, right, bottom in mask_rects(_visible_windows(), patterns):
        draw.rectangle((left, top, right - 1, bottom - 1), fill=(0, 0, 0))
    return image


def _encode(image, scope: str) -> dict:
    image = image.convert("RGB")
    width, height = fit_size(*image.size)
    if (width, height) != image.size:
        image = image.resize((width, height))
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=JPEG_QUALITY)
    return {
        "image_b64": base64.b64encode(buf.getvalue()).decode(),
        "size": (width, height),
        "scope": scope,
    }


# ------------------------------------------------------------- Win32 access

class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32),
    ]


def _win32():
    """user32 and gdi32, with handle-sized argument types. Windows only.

    Handles are declared as pointers so a 64-bit handle is never truncated to a C int.
    """
    global _bound
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    if _bound:
        return user32, gdi32
    _bound = True
    hwnd = ctypes.c_void_p
    user32.IsWindow.argtypes = [hwnd]
    user32.GetWindowRect.argtypes = [hwnd, ctypes.c_void_p]
    user32.GetWindowTextLengthW.argtypes = [hwnd]
    user32.GetWindowTextW.argtypes = [hwnd, ctypes.c_void_p, ctypes.c_int]
    user32.GetClassNameW.argtypes = [hwnd, ctypes.c_void_p, ctypes.c_int]
    user32.IsIconic.argtypes = [hwnd]
    user32.IsWindowVisible.argtypes = [hwnd]
    user32.PrintWindow.argtypes = [hwnd, ctypes.c_void_p, ctypes.c_uint]
    user32.GetDC.argtypes = [hwnd]
    user32.GetDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [hwnd, ctypes.c_void_p]
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    gdi32.GetDIBits.restype = ctypes.c_int
    return user32, gdi32


def _window_info(hwnd: int) -> WindowInfo | None:
    user32, _ = _win32()
    if not user32.IsWindow(hwnd):
        return None
    rect = _Rect()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    title = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title, length + 1)
    class_name = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, class_name, 256)
    return WindowInfo(
        handle=int(hwnd),
        title=title.value,
        class_name=class_name.value,
        rect=(rect.left, rect.top, rect.right, rect.bottom),
        minimized=bool(user32.IsIconic(hwnd)),
    )


def _visible_windows() -> list[WindowInfo]:
    """Visible, restored top-level windows. Minimized ones are not on screen, so they cannot leak."""
    user32, _ = _win32()
    handles: list[int] = []

    def visit(hwnd, _lparam):
        handles.append(hwnd)
        return True

    callback = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(visit)
    user32.EnumWindows(callback, None)
    found = []
    for hwnd in handles:
        if not user32.IsWindowVisible(hwnd):
            continue
        info = _window_info(hwnd)
        if info is not None and not info.minimized:
            found.append(info)
    return found


def _print_window(hwnd: int, width: int, height: int):
    """Render a window's own content into a bitmap, without moving it or taking focus."""
    from PIL import Image

    user32, gdi32 = _win32()
    screen = user32.GetDC(None)
    memory = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
    previous = gdi32.SelectObject(memory, bitmap)
    try:
        if not user32.PrintWindow(hwnd, memory, PW_RENDERFULLCONTENT):
            return None
        # A negative height asks for a top-down bitmap, so rows come out in screen order.
        header = _BitmapInfoHeader(
            biSize=ctypes.sizeof(_BitmapInfoHeader), biWidth=width, biHeight=-height,
            biPlanes=1, biBitCount=32, biCompression=0,
        )
        pixels = ctypes.create_string_buffer(width * height * 4)
        if gdi32.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.byref(header), 0) != height:
            return None
        return Image.frombuffer("RGB", (width, height), pixels.raw, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(memory, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(None, screen)


def _grab_primary():
    from PIL import ImageGrab

    return ImageGrab.grab()
