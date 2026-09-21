"""System-level controls: master volume, and per-window state.

Everything here is a native Windows API call - no mouse, no screenshot. The
volume path goes through Core Audio (IAudioEndpointVolume), and window state
reuses the UIA WindowPattern the desktop layer already exposes, so this module
stays small and leans on infrastructure that is already tested.

Two design choices worth stating:

  Reads never change anything and are always allowed. "How loud is it" and
  "what state is this window in" are safe to answer without a capability, but
  the tools that expose them still sit behind CAP_SYSTEM so the surface is one
  toggle, not several.

  Closing a window is the one irreversible action in here, and it is the one
  the confirmation flow in brain.py gates. Volume and minimize/maximize are
  trivially reversible, so they just run.
"""
from __future__ import annotations

import logging

log = logging.getLogger("system")

# WindowVisualState enum values from UIAutomation.
WV_NORMAL = 0
WV_MAXIMIZED = 1
WV_MINIMIZED = 2

STATE_NAMES = {
    "maximize": WV_MAXIMIZED, "max": WV_MAXIMIZED, "katta": WV_MAXIMIZED,
    "toliq": WV_MAXIMIZED, "kattalashtir": WV_MAXIMIZED,
    "minimize": WV_MINIMIZED, "min": WV_MINIMIZED, "kichik": WV_MINIMIZED,
    "yashir": WV_MINIMIZED, "pastga": WV_MINIMIZED,
    "normal": WV_NORMAL, "restore": WV_NORMAL, "tikla": WV_NORMAL,
    "odatiy": WV_NORMAL,
}


def available() -> bool:
    import importlib.util
    return bool(importlib.util.find_spec("pycaw") and importlib.util.find_spec("comtypes"))


def status() -> str:
    if not available():
        return "ovoz boshqaruvi o'rnatilmagan — pip install pycaw comtypes"
    return "tayyor"


# --------------------------------------------------------------------- volume

def _with_audio(fn):
    """Run `fn(endpoint)` on the shared COM thread, releasing COM there too.

    Two rules, both learned by crashing:

    One apartment. Core Audio objects are apartment-bound, so every call is
    marshalled onto the single uia worker thread rather than the asyncio
    executor pool.

    Release on the creating thread. This is the one that actually bit: the
    endpoint was built on the worker thread but its refcount dropped later,
    so Python finalized it on whichever thread happened to run the collection.
    Releasing a COM pointer off its apartment gave "COM method call without
    VTable" and then a segfault - the crash appeared as a window read failing
    after a volume change, which is nowhere near the real cause. The objects
    are therefore created, used, dropped and collected inside this one job.
    """
    from . import uia

    def job():
        import gc
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL, CoCreateInstance, GUID
        from pycaw.pycaw import (
            EDataFlow, ERole, IAudioEndpointVolume, IMMDeviceEnumerator,
        )

        # Built by hand through MMDeviceEnumerator: the pycaw convenience
        # helper (AudioUtilities.GetSpeakers().Activate) is broken on the
        # installed version - GetSpeakers returns a wrapper with no Activate.
        enumerator = CoCreateInstance(
            GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
            IMMDeviceEnumerator, CLSCTX_ALL,
        )
        device = enumerator.GetDefaultAudioEndpoint(
            EDataFlow.eRender.value, ERole.eMultimedia.value
        )
        iface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(iface, POINTER(IAudioEndpointVolume))
        try:
            return fn(volume)
        finally:
            del volume, iface, device, enumerator
            gc.collect()      # finalize here, on the apartment that owns them

    return uia._worker.call(job)


def get_volume() -> dict:
    if not available():
        return {"error": status()}

    def job(vol):
        return {
            "percent": round(vol.GetMasterVolumeLevelScalar() * 100),
            "muted": bool(vol.GetMute()),
        }

    try:
        return _with_audio(job)
    except Exception as exc:
        return {"error": f"Ovozni o'qib bo'lmadi: {exc}"}


def set_volume(percent: float) -> dict:
    """Set the master volume to an absolute percentage (0-100)."""
    if not available():
        return {"error": status()}
    target = max(0.0, min(100.0, float(percent)))

    def job(vol):
        if vol.GetMute():
            vol.SetMute(0, None)          # setting a level implies unmute
        vol.SetMasterVolumeLevelScalar(target / 100.0, None)
        return {"ok": True, "percent": round(vol.GetMasterVolumeLevelScalar() * 100)}

    try:
        return _with_audio(job)
    except Exception as exc:
        return {"error": f"Ovozni o'zgartirib bo'lmadi: {exc}"}


def adjust_volume(delta: float) -> dict:
    """Relative change, e.g. +30 or -10. "make it louder by 30%" -> +30."""
    if not available():
        return {"error": status()}

    def job(vol):
        current = vol.GetMasterVolumeLevelScalar() * 100
        target = max(0.0, min(100.0, current + float(delta)))
        if vol.GetMute() and delta > 0:
            vol.SetMute(0, None)
        vol.SetMasterVolumeLevelScalar(target / 100.0, None)
        return {
            "ok": True,
            "from": round(current),
            "percent": round(vol.GetMasterVolumeLevelScalar() * 100),
        }

    try:
        return _with_audio(job)
    except Exception as exc:
        return {"error": f"Ovozni o'zgartirib bo'lmadi: {exc}"}


def set_mute(mute: bool) -> dict:
    if not available():
        return {"error": status()}

    def job(vol):
        vol.SetMute(1 if mute else 0, None)
        return {"ok": True, "muted": bool(vol.GetMute())}

    try:
        return _with_audio(job)
    except Exception as exc:
        return {"error": f"Xato: {exc}"}
