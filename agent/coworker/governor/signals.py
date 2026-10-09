"""Operating-system readings for the governor, from psutil and a few Win32 calls.

Every reading is wrapped. A failed reading returns the value that does the least
harm when the probe is broken: idle 0 (owner present, so background work pauses),
CPU 0 and RAM treated as plenty (a broken probe must not refuse work), power
unknown but not battery saver, elevation False, and the input desktop assumed
available unless the probe itself reports that it is not.

The thread helpers at the bottom run inside the governor's worker threads. They
do nothing off Windows.
"""
from __future__ import annotations

import ctypes
import functools
import logging
import sys
import threading
import time
from ctypes import wintypes
from types import SimpleNamespace
from typing import Any, Callable, Optional

import psutil

from ..core.ports import PowerState

log = logging.getLogger("governor")

IS_WINDOWS = sys.platform == "win32"
MIB = 1024 * 1024
CPU_SAMPLE_S = 0.5     # psutil averages over this interval; it blocks the caller that long
CPU_CACHE_S = 1.0      # a fresh CPU reading is reused for this long
PLENTY_MB = float(1 << 20)  # 1 TiB in MiB: what a failed RAM probe reports

_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION = 20                 # TOKEN_INFORMATION_CLASS value for TokenElevation
_DESKTOP_SWITCHDESKTOP = 0x0100
_COINIT_APARTMENTTHREADED = 0x2
_THREAD_MODE_BACKGROUND_BEGIN = 0x00010000
_BATTERY_SAVER_ON = 1                 # SystemStatusFlag bit reported by Windows 10 1709 and later


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class _TokenElevation(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


def _declare(func: Any, restype: Any, *argtypes: Any) -> None:
    # Handles and pointers are pointer-sized. Without declared types ctypes passes
    # them as C ints and truncates them on 64-bit Windows.
    func.restype = restype
    func.argtypes = list(argtypes)


def _load_win32() -> SimpleNamespace:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32")
    _declare(user32.GetLastInputInfo, wintypes.BOOL, ctypes.POINTER(_LastInputInfo))
    _declare(user32.OpenInputDesktop, wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _declare(user32.CloseDesktop, wintypes.BOOL, wintypes.HANDLE)
    _declare(kernel32.GetTickCount64, ctypes.c_ulonglong)
    _declare(kernel32.GetCurrentProcess, wintypes.HANDLE)
    _declare(kernel32.GetCurrentThread, wintypes.HANDLE)
    _declare(kernel32.SetThreadPriority, wintypes.BOOL, wintypes.HANDLE, ctypes.c_int)
    _declare(kernel32.CloseHandle, wintypes.BOOL, wintypes.HANDLE)
    _declare(kernel32.GetSystemPowerStatus, wintypes.BOOL, ctypes.POINTER(_SystemPowerStatus))
    _declare(advapi32.OpenProcessToken, wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    _declare(advapi32.GetTokenInformation, wintypes.BOOL, wintypes.HANDLE, ctypes.c_int,
             ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    _declare(ole32.CoInitializeEx, ctypes.c_long, ctypes.c_void_p, wintypes.DWORD)
    return SimpleNamespace(user32=user32, kernel32=kernel32, advapi32=advapi32, ole32=ole32)


@functools.cache
def _win32() -> SimpleNamespace:
    """The loaded Win32 libraries. Raises off Windows; callers turn that into a default."""
    return _load_win32()


def idle_seconds_from_ticks(tick_ms: int, last_input_ms: int) -> float:
    """Seconds since the last input, from GetTickCount64 and LASTINPUTINFO.dwTime.

    dwTime is a 32-bit tick count that wraps about every 49.7 days, so the
    difference is taken modulo 2**32 whatever the 64-bit tick count is.
    """
    return ((tick_ms - last_input_ms) & 0xFFFFFFFF) / 1000.0


def _battery_saver() -> bool:
    try:
        status = _SystemPowerStatus()
        if not _win32().kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return False
        return status.SystemStatusFlag == _BATTERY_SAVER_ON
    except Exception:
        log.debug("battery saver probe failed", exc_info=True)
        return False


class RealOs:
    """The OsPort the application uses. Implements ``core.ports.OsPort``."""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._cpu_lock = threading.Lock()
        self._cpu_value = 0.0
        self._cpu_at: float | None = None

    def idle_seconds(self) -> float:
        try:
            api = _win32()
            info = _LastInputInfo()
            info.cbSize = ctypes.sizeof(info)
            if not api.user32.GetLastInputInfo(ctypes.byref(info)):
                return 0.0
            return idle_seconds_from_ticks(api.kernel32.GetTickCount64(), info.dwTime)
        except Exception:
            log.debug("idle probe failed; the owner is treated as present", exc_info=True)
            return 0.0

    def cpu_percent(self) -> float:
        # Held across the blocking sample so concurrent callers share one reading.
        with self._cpu_lock:
            now = self._monotonic()
            if self._cpu_at is not None and now - self._cpu_at < CPU_CACHE_S:
                return self._cpu_value
            try:
                value = float(psutil.cpu_percent(interval=CPU_SAMPLE_S))
            except Exception:
                log.debug("CPU probe failed", exc_info=True)
                value = 0.0
            self._cpu_value, self._cpu_at = value, now
            return value

    def free_ram_mb(self) -> float:
        try:
            return psutil.virtual_memory().available / MIB
        except Exception:
            log.debug("RAM probe failed; treated as plenty", exc_info=True)
            return PLENTY_MB

    def power(self) -> PowerState:
        percent: int | None = None
        plugged: bool | None = None
        try:
            battery = psutil.sensors_battery()
            if battery is not None:
                percent = round(battery.percent)
                plugged = battery.power_plugged
        except Exception:
            log.debug("battery probe failed; power state unknown", exc_info=True)
        return PowerState(percent=percent, plugged=plugged, saver=_battery_saver())

    def is_elevated(self) -> Optional[bool]:
        try:
            api = _win32()
            token = wintypes.HANDLE()
            if not api.advapi32.OpenProcessToken(api.kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
                return None
            try:
                info = _TokenElevation()
                returned = wintypes.DWORD()
                ok = api.advapi32.GetTokenInformation(
                    token.value, _TOKEN_ELEVATION, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(returned),
                )
                if not ok:
                    return None
                return info.TokenIsElevated != 0
            finally:
                api.kernel32.CloseHandle(token.value)
        except Exception:
            log.debug("elevation probe failed; the answer is unknown", exc_info=True)
            return None

    def input_desktop_available(self) -> bool:
        # A probe that cannot run says nothing about the desktop, so it does not block
        # input. Only a clean NULL from OpenInputDesktop means the lock screen is up.
        try:
            api = _win32()
            desktop = api.user32.OpenInputDesktop(0, False, _DESKTOP_SWITCHDESKTOP)
        except Exception:
            log.debug("input desktop probe failed; treated as available", exc_info=True)
            return True
        if not desktop:
            return False
        try:
            return True
        finally:
            api.user32.CloseDesktop(desktop)


def lower_thread_priority() -> bool:
    """Put the calling thread into background mode, the way Windows background I/O does.

    Used as the initializer of the background pools (INDEX, VISION, STT). Returns
    False when the call did not apply, which is always the case off Windows.
    """
    if not IS_WINDOWS:
        return False
    try:
        api = _win32()
        return bool(api.kernel32.SetThreadPriority(api.kernel32.GetCurrentThread(), _THREAD_MODE_BACKGROUND_BEGIN))
    except Exception:
        log.debug("could not enter background thread mode", exc_info=True)
        return False


def init_com_apartment() -> None:
    """Join the calling thread to a single-threaded COM apartment (Windows).

    UI Automation objects belong to the thread that created them, so the UIA pool's
    thread is initialised before any automation call runs on it.
    """
    if not IS_WINDOWS:
        return
    try:
        _win32().ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    except Exception:
        log.debug("COM apartment initialisation failed", exc_info=True)
