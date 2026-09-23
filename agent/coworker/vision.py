"""Reading a screen with a vision model - the phase-3 fallback.

Scope was set by measurement, not ambition. DeepSeek's vision model
(deepseek-flash) describes a screen accurately - "dark background, blue Login
button" - so it is genuinely useful for the screens the UIA layer returns
empty for: Electron apps, canvas UIs, games, embedded viewers. But its pixel
*coordinate* grounding is unreliable and token-expensive, exactly as the
research warned, so this module deliberately does READING only. It does not
click by pixel; that would demo once and then fail.

Capture is local (PIL.ImageGrab). Only the resulting screenshot is sent to the
model, and only when the user asked to read a screen - never continuously.
"""
from __future__ import annotations

import base64
import io
import logging

log = logging.getLogger("vision")

# DeepSeek downscales images to ~1024 tokens anyway; keep the longest side
# modest so the upload is small and the cost stays near zero.
MAX_SIDE = 1400


def available() -> bool:
    import importlib.util
    return bool(importlib.util.find_spec("PIL"))


def status() -> str:
    if not available():
        return "o'rnatilmagan — pip install pillow"
    return "tayyor"


def capture(title: str = "", handle: int = 0) -> dict:
    """Grab the whole screen, or one window's region by title.

    Returns {"image_b64", "size", "scope"} or {"error"}.
    """
    if not available():
        return {"error": status()}

    from PIL import ImageGrab

    bbox = None
    scope = "butun ekran"
    if handle:
        rect = _window_rect("", handle)
        if rect is None:
            return {"error": "Oyna topilmadi."}
        bbox = rect
        scope = _title_of(handle) or "oyna"
    elif title:
        rect = _window_rect(title)
        if rect is None:
            return {"error": f"«{title}» oynasi topilmadi."}
        bbox = rect
        scope = title

    try:
        img = ImageGrab.grab(bbox=bbox)
    except Exception as exc:
        return {"error": f"Skrinshot olinmadi: {exc}"}

    # Downscale so the longest side is at most MAX_SIDE.
    w, h = img.size
    longest = max(w, h)
    if longest > MAX_SIDE:
        scale = MAX_SIDE / longest
        img = img.resize((int(w * scale), int(h * scale)))

    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=80)
    return {
        "image_b64": base64.b64encode(buf.getvalue()).decode(),
        "size": img.size,
        "scope": scope,
    }


def _title_of(handle: int) -> str:
    from . import uia
    info = uia.window_by_handle(handle)
    return (info or {}).get("title", "") if info else ""


def _window_rect(title: str, handle: int = 0):
    """A window's on-screen rectangle, via the UIA layer."""
    from . import uia

    if not uia.available():
        return None

    def job():
        import uiautomation as auto

        auto.SetGlobalSearchTimeout(1)
        win = uia._locate(auto, title, handle)
        if win is None:
            return None
        try:
            r = win.BoundingRectangle
            if r.width() <= 0 or r.height() <= 0:
                return None
            # Bring it forward so the capture is not of a covered window.
            try:
                if win.GetWindowPattern().WindowVisualState == 2:
                    win.GetWindowPattern().SetWindowVisualState(0)
                win.SetFocus()
            except Exception:
                pass
            return (r.left, r.top, r.right, r.bottom)
        except Exception:
            return None

    try:
        rect = uia._worker.call(job, timeout=15)
    except Exception:
        return None
    if rect is None:
        return None
    # A tiny settle so the focus/restore has repainted before the grab.
    import time

    time.sleep(0.3)
    return rect


DEFAULT_PROMPT = (
    "Bu kompyuter ekranining rasmi. Foydalanuvchi savoliga QISQA javob ber. "
    "Ekranda nima ko'rinayotganini ayt: matn, tugmalar, xatolik xabari, holat. "
    "Ko'rmagan narsangni o'ylab topma."
)
