"""System controls: master volume, window state constants, lock, power, processes and status.

Everything here is a native Windows call. Nothing is a shell: commands run from
fixed argument lists with shell=False, and Windows binaries are named by absolute
path, so a file planted in the working directory cannot stand in for them.

Two rules shape the module:

  Values are validated here as well as in the tool schema. The schema is what the
  model sees; this check protects the machine if a caller skips the schema. Non-
  finite numbers are refused because NaN passes every comparison and would reach
  the audio driver as a volume.

  Reads change nothing, and the process list carries names and pids only. Toolhelp32
  does not expose command lines at all, and list_processes keeps just those two
  fields, so arguments that may hold tokens never leave this module.

Lock and power end the session, so the tool layer always asks the owner first;
this module performs them only when it is called.
"""
from __future__ import annotations

import ctypes
import gc
import logging
import math
import os
import shutil
import subprocess
from ctypes import wintypes
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from . import uia

if TYPE_CHECKING:  # pragma: no cover
    from .core.ports import OsPort

log = logging.getLogger("system")

# A Core Audio call that does not return must not hold the shared COM thread
# forever. The caller gets TimeoutError after this many seconds.
AUDIO_TIMEOUT_S = 10.0

PERCENT_RANGE = (0.0, 100.0)
DELTA_RANGE = (-100.0, 100.0)

POWER_ACTIONS = ("sleep", "restart", "shutdown")
POWER_LABELS = {"sleep": "uyquga o'tkazish", "restart": "qayta yuklash", "shutdown": "o'chirish"}

_NOT_WINDOWS = "Faqat Windows"
_CREATE_NO_WINDOW = 0x08000000
_GIB = 1024 ** 3


class WindowVisualState(IntEnum):
    """WindowVisualState values of UI Automation's WindowPattern."""

    NORMAL = 0
    MAXIMIZED = 1
    MINIMIZED = 2


# ----------------------------------------------------------------- validation

def _finite_in_range(value: Any, label: str, bounds: tuple[float, float]) -> float:
    # bool is an int subclass, and True would silently become a 1.0 volume.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} raqam bo'lishi kerak")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{label} juda katta") from None
    if not math.isfinite(number):
        raise ValueError(f"{label} chekli raqam bo'lishi kerak")
    low, high = bounds
    if not low <= number <= high:
        raise ValueError(f"{label} {low:g} dan {high:g} gacha bo'lishi kerak")
    return number


def validate_percent(value: Any) -> float:
    """An absolute volume, finite and between 0 and 100. Raises ValueError otherwise."""
    return _finite_in_range(value, "Ovoz foizi", PERCENT_RANGE)


def validate_delta(value: Any) -> float:
    """A relative volume change, finite and between -100 and 100. Raises ValueError otherwise."""
    return _finite_in_range(value, "Ovoz o'zgarishi", DELTA_RANGE)


# --------------------------------------------------------------------- volume

def available() -> bool:
    import importlib.util
    return bool(importlib.util.find_spec("pycaw") and importlib.util.find_spec("comtypes"))


def audio_status() -> str:
    if not available():
        return "ovoz boshqaruvi o'rnatilmagan — pip install pycaw comtypes"
    return "tayyor"


def _with_audio(fn):
    """Run ``fn(endpoint)`` on the shared COM thread, with the COM objects released there too.

    Core Audio objects are apartment-bound, so every call is marshalled onto the
    single uia worker thread. The objects are also created, used and dropped inside
    one job: a COM pointer released on another thread crashed the process earlier,
    which looked like a window read failing after a volume change.
    """

    def job():
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL, CoCreateInstance, GUID
        from pycaw.pycaw import (
            EDataFlow, ERole, IAudioEndpointVolume, IMMDeviceEnumerator,
        )

        # Built by hand through MMDeviceEnumerator: the pycaw convenience helper
        # (AudioUtilities.GetSpeakers().Activate) is broken on the installed version.
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

    return uia._worker.call(job, timeout=AUDIO_TIMEOUT_S)


def get_volume() -> dict:
    if not available():
        return {"error": audio_status()}

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
        return {"error": audio_status()}
    try:
        target = validate_percent(percent)
    except ValueError as exc:
        return {"error": str(exc)}

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
        return {"error": audio_status()}
    try:
        step = validate_delta(delta)
    except ValueError as exc:
        return {"error": str(exc)}

    def job(vol):
        current = vol.GetMasterVolumeLevelScalar() * 100
        target = max(0.0, min(100.0, current + step))
        if vol.GetMute() and step > 0:
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
        return {"error": audio_status()}

    def job(vol):
        vol.SetMute(1 if mute else 0, None)
        return {"ok": True, "muted": bool(vol.GetMute())}

    try:
        return _with_audio(job)
    except Exception as exc:
        return {"error": f"Xato: {exc}"}


# --------------------------------------------------------------- lock and power

def lock_workstation() -> dict:
    if os.name != "nt":
        return {"error": _NOT_WINDOWS}
    if not ctypes.WinDLL("user32").LockWorkStation():
        return {"error": "Ekranni qulflab bo'lmadi."}
    return {"ok": True}


def power_action(action: str) -> dict:
    """Sleep, restart or shut down. Running apps may still refuse a restart or shutdown."""
    if action not in POWER_ACTIONS:
        return {"error": "Noma'lum quvvat amali."}
    if os.name != "nt":
        return {"error": _NOT_WINDOWS}
    if action == "sleep":
        done = _suspend_system()
    else:
        done = _run_shutdown("/r" if action == "restart" else "/s")
    if not done:
        return {"error": f"Kompyuterni {POWER_LABELS[action]} bo'lmadi."}
    return {"ok": True, "action": action}


def _suspend_system() -> bool:
    # SetSuspendState(hibernate, force, wake_events_disabled). Force stays False so
    # apps that hold unsaved work can still veto the sleep.
    return bool(ctypes.WinDLL("powrprof").SetSuspendState(0, 0, 0))


def _run_shutdown(flag: str) -> bool:
    # No /f: running apps get the normal end-of-session prompt instead of being killed.
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "shutdown.exe")
    proc = subprocess.run(
        [exe, flag, "/t", "0"], shell=False, capture_output=True, timeout=15,
        creationflags=_CREATE_NO_WINDOW, check=False,
    )
    return proc.returncode == 0


# ------------------------------------------------------------------ processes

TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _enumerate_processes() -> list[dict]:
    """Every running process as {pid, name} from a Toolhelp32 snapshot."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateToolhelp32Snapshot
    create.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create.restype = wintypes.HANDLE
    first, nxt = kernel32.Process32FirstW, kernel32.Process32NextW
    first.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    nxt.argtypes = first.argtypes
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snap = create(TH32CS_SNAPPROCESS, 0)
    if snap is None or snap == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        rows: list[dict] = []
        ok = first(snap, ctypes.byref(entry))
        while ok:
            rows.append({"pid": entry.th32ProcessID, "name": entry.szExeFile})
            ok = nxt(snap, ctypes.byref(entry))
        return rows
    finally:
        kernel32.CloseHandle(snap)


def list_processes(limit: int) -> dict:
    """Up to ``limit`` processes sorted by name, with the total count. Names and pids only."""
    rows = sorted(_enumerate_processes(), key=lambda r: (r["name"].lower(), r["pid"]))
    shown = [{"pid": r["pid"], "name": r["name"]} for r in rows[:limit]]
    return {"processes": shown, "total": len(rows)}


# --------------------------------------------------------------------- status

def uptime_seconds() -> int:
    kernel32 = ctypes.WinDLL("kernel32")
    get_ticks = kernel32.GetTickCount64
    get_ticks.restype = ctypes.c_ulonglong      # the default int would truncate
    return int(get_ticks()) // 1000


def disk_summary() -> dict:
    drive = os.environ.get("SystemDrive", "C:")
    usage = shutil.disk_usage(drive + os.sep)
    return {
        "drive": drive,
        "total_gb": round(usage.total / _GIB, 1),
        "free_gb": round(usage.free / _GIB, 1),
        "used_percent": round(100 * usage.used / usage.total),
    }


def system_snapshot(port: "OsPort") -> dict:
    """CPU, free memory and battery come from the OS port; disk and uptime are read directly.

    The OsPort protocol has no disk or uptime signal, and it belongs to the core
    layer, so those two are read here.
    """
    power = port.power()
    battery = None
    if power.percent is not None:
        battery = {"percent": power.percent, "plugged": power.plugged, "saver": power.saver}
    return {
        "cpu_percent": round(port.cpu_percent()),
        "ram_free_mb": round(port.free_ram_mb()),
        "battery": battery,
        "disk": disk_summary(),
        "uptime_s": uptime_seconds(),
    }
