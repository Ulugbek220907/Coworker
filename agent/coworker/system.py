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

def _endpoint():
    """Default render device's IAudioEndpointVolume.

    Built by hand through the MMDeviceEnumerator because the pycaw convenience
    helper (AudioUtilities.GetSpeakers().Activate) is broken on the installed
    version - GetSpeakers returns a wrapper with no Activate. This path is the
    stable one.
    """
    from ctypes import POINTER, cast

    from comtypes import CLSCTX_ALL, CoCreateInstance, GUID
    from pycaw.pycaw import EDataFlow, ERole, IAudioEndpointVolume, IMMDeviceEnumerator

    enumerator = CoCreateInstance(
        GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
        IMMDeviceEnumerator, CLSCTX_ALL,
    )
    device = enumerator.GetDefaultAudioEndpoint(
        EDataFlow.eRender.value, ERole.eMultimedia.value
    )
    iface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


def _with_audio(fn):
    """Marshal an audio operation onto the shared COM thread.

    Core Audio (pycaw) and UI Automation both go through comtypes, whose lazy
    type-library codegen is not thread-safe. Two separate STA worker threads
    triggering that codegen at once segfaults - it was reproducible here the
    first time list_windows ran after a volume call. Routing both through the
    one uia worker thread means all COM work is serialised on a single
    apartment, so there is never a concurrent-codegen race.
    """
    from . import uia

    return uia._worker.call(fn)


def get_volume() -> dict:
    if not available():
        return {"error": status()}

    def job():
        vol = _endpoint()
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

    def job():
        vol = _endpoint()
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

    def job():
        vol = _endpoint()
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

    def job():
        vol = _endpoint()
        vol.SetMute(1 if mute else 0, None)
        return {"ok": True, "muted": bool(vol.GetMute())}

    try:
        return _with_audio(job)
    except Exception as exc:
        return {"error": f"Xato: {exc}"}
