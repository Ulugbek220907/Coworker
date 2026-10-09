"""RealOs: psutil and Win32 readings, and the safe default each one falls back to.

Win32 is replaced by small fakes that fill the byref buffers the real calls fill, so
the parsing and the fallbacks are tested on any platform without reading the machine.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from coworker.core.ports import PowerState
from coworker.governor import signals
from coworker.governor.signals import MIB, PLENTY_MB, RealOs, idle_seconds_from_ticks


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def broken_win32() -> SimpleNamespace:
    raise OSError("Win32 is not available in this test")


@pytest.fixture
def no_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(signals, "_win32", broken_win32)


def fake_win32(**libraries: object) -> SimpleNamespace:
    return SimpleNamespace(**libraries)


def test_real_os_provides_every_method_of_the_port():
    os = RealOs()
    for name in ("idle_seconds", "cpu_percent", "free_ram_mb", "power", "is_elevated", "input_desktop_available"):
        assert callable(getattr(os, name))


# ----------------------------------------------------------------- idle time

def test_idle_seconds_come_from_the_tick_difference():
    assert idle_seconds_from_ticks(10_000, 4_000) == 6.0


def test_idle_seconds_survive_the_32_bit_tick_wraparound():
    assert idle_seconds_from_ticks(2**32 + 5_000, 1_000) == 4.0
    assert idle_seconds_from_ticks(2**32 + 500, 2**32 - 500) == 1.0


def test_idle_is_read_from_get_last_input_info(monkeypatch):
    def get_last_input_info(buf):
        buf._obj.dwTime = 4_000
        return 1

    fake = fake_win32(
        user32=SimpleNamespace(GetLastInputInfo=get_last_input_info),
        kernel32=SimpleNamespace(GetTickCount64=lambda: 10_000),
    )
    monkeypatch.setattr(signals, "_win32", lambda: fake)
    assert RealOs().idle_seconds() == 6.0


def test_idle_is_zero_when_get_last_input_info_fails(monkeypatch):
    fake = fake_win32(
        user32=SimpleNamespace(GetLastInputInfo=lambda buf: 0),
        kernel32=SimpleNamespace(GetTickCount64=lambda: 10_000),
    )
    monkeypatch.setattr(signals, "_win32", lambda: fake)
    assert RealOs().idle_seconds() == 0.0


def test_idle_probe_failure_reports_the_owner_as_present(no_win32):
    assert RealOs().idle_seconds() == 0.0


# ----------------------------------------------------------------------- CPU

def test_cpu_reading_is_sampled_over_half_a_second_and_cached_for_one(monkeypatch):
    calls: list[float] = []

    def fake_cpu_percent(interval: float) -> float:
        calls.append(interval)
        return 42.0

    monkeypatch.setattr(signals.psutil, "cpu_percent", fake_cpu_percent)
    clock = FakeClock(0.0)
    os = RealOs(monotonic=clock)
    assert os.cpu_percent() == 42.0
    clock.now = 0.5
    assert os.cpu_percent() == 42.0
    assert len(calls) == 1
    clock.now = 1.5
    assert os.cpu_percent() == 42.0
    assert calls == [signals.CPU_SAMPLE_S, signals.CPU_SAMPLE_S]


def test_cpu_probe_failure_reads_zero(monkeypatch):
    def failing_cpu_percent(interval: float) -> float:
        raise RuntimeError("no counters")

    monkeypatch.setattr(signals.psutil, "cpu_percent", failing_cpu_percent)
    assert RealOs().cpu_percent() == 0.0


# ----------------------------------------------------------------------- RAM

def test_free_ram_is_reported_in_mib(monkeypatch):
    monkeypatch.setattr(signals.psutil, "virtual_memory", lambda: SimpleNamespace(available=1536 * MIB))
    assert RealOs().free_ram_mb() == 1536.0


def test_ram_probe_failure_is_treated_as_plenty(monkeypatch):
    def failing_memory():
        raise RuntimeError("no memory counters")

    monkeypatch.setattr(signals.psutil, "virtual_memory", failing_memory)
    assert RealOs().free_ram_mb() == PLENTY_MB
    assert PLENTY_MB > 1536


# --------------------------------------------------------------------- power

def test_battery_percent_and_plugged_state_come_from_psutil(monkeypatch, no_win32):
    monkeypatch.setattr(
        signals.psutil, "sensors_battery",
        lambda: SimpleNamespace(percent=87.4, power_plugged=True),
    )
    assert RealOs().power() == PowerState(percent=87, plugged=True, saver=False)


def test_a_machine_without_a_battery_has_unknown_power(monkeypatch, no_win32):
    monkeypatch.setattr(signals.psutil, "sensors_battery", lambda: None)
    assert RealOs().power() == PowerState(percent=None, plugged=None, saver=False)


def test_power_probe_failure_is_unknown_and_not_battery_saver(monkeypatch, no_win32):
    def failing_battery():
        raise RuntimeError("no battery interface")

    monkeypatch.setattr(signals.psutil, "sensors_battery", failing_battery)
    assert RealOs().power() == PowerState(percent=None, plugged=None, saver=False)


def test_battery_saver_is_read_from_system_power_status(monkeypatch):
    def get_system_power_status(buf):
        buf._obj.SystemStatusFlag = 1
        return 1

    fake = fake_win32(kernel32=SimpleNamespace(GetSystemPowerStatus=get_system_power_status))
    monkeypatch.setattr(signals, "_win32", lambda: fake)
    monkeypatch.setattr(signals.psutil, "sensors_battery", lambda: None)
    assert RealOs().power().saver is True


# ---------------------------------------------------------------- elevation

def test_elevation_is_read_from_the_process_token(monkeypatch):
    closed: list[int] = []

    def open_process_token(process, access, token_ref):
        token_ref._obj.value = 1234
        return 1

    def get_token_information(handle, token_class, buf, size, returned):
        assert handle == 1234
        assert token_class == 20          # TokenElevation
        buf._obj.TokenIsElevated = 1
        return 1

    fake = fake_win32(
        advapi32=SimpleNamespace(OpenProcessToken=open_process_token, GetTokenInformation=get_token_information),
        kernel32=SimpleNamespace(GetCurrentProcess=lambda: -1, CloseHandle=closed.append),
    )
    monkeypatch.setattr(signals, "_win32", lambda: fake)
    assert RealOs().is_elevated() is True
    assert closed == [1234]


def test_elevation_probe_failure_is_unknown(no_win32):
    assert RealOs().is_elevated() is None


# ------------------------------------------------------------- input desktop

def test_a_null_input_desktop_means_the_desktop_is_locked(monkeypatch):
    closed: list[int] = []
    fake = fake_win32(user32=SimpleNamespace(OpenInputDesktop=lambda *args: None, CloseDesktop=closed.append))
    monkeypatch.setattr(signals, "_win32", lambda: fake)
    assert RealOs().input_desktop_available() is False
    assert closed == []


def test_an_input_desktop_that_opens_is_closed_again(monkeypatch):
    closed: list[int] = []
    fake = fake_win32(user32=SimpleNamespace(OpenInputDesktop=lambda *args: 4321, CloseDesktop=closed.append))
    monkeypatch.setattr(signals, "_win32", lambda: fake)
    assert RealOs().input_desktop_available() is True
    assert closed == [4321]


def test_an_input_desktop_probe_that_cannot_run_does_not_block_input(no_win32):
    assert RealOs().input_desktop_available() is True


# --------------------------------------------------------------- thread setup

def test_thread_setup_does_nothing_off_windows(monkeypatch):
    calls: list[str] = []

    def recording_win32():
        calls.append("win32")
        raise OSError("off Windows")

    monkeypatch.setattr(signals, "IS_WINDOWS", False)
    monkeypatch.setattr(signals, "_win32", recording_win32)
    assert signals.lower_thread_priority() is False
    assert signals.init_com_apartment() is None
    assert calls == []


def test_background_mode_is_begun_for_the_calling_thread_on_windows(monkeypatch):
    priorities: list[tuple[int, int]] = []

    def set_thread_priority(handle: int, priority: int) -> int:
        priorities.append((handle, priority))
        return 1

    fake = fake_win32(kernel32=SimpleNamespace(GetCurrentThread=lambda: 77, SetThreadPriority=set_thread_priority))
    monkeypatch.setattr(signals, "IS_WINDOWS", True)
    monkeypatch.setattr(signals, "_win32", lambda: fake)
    assert signals.lower_thread_priority() is True
    assert priorities == [(77, 0x00010000)]   # THREAD_MODE_BACKGROUND_BEGIN


def test_background_mode_failure_returns_false(monkeypatch, no_win32):
    monkeypatch.setattr(signals, "IS_WINDOWS", True)
    assert signals.lower_thread_priority() is False


def test_com_apartment_is_initialised_as_single_threaded_on_windows(monkeypatch):
    calls: list[tuple[object, int]] = []

    def co_initialize_ex(reserved: object, flags: int) -> int:
        calls.append((reserved, flags))
        return 0

    fake = fake_win32(ole32=SimpleNamespace(CoInitializeEx=co_initialize_ex))
    monkeypatch.setattr(signals, "IS_WINDOWS", True)
    monkeypatch.setattr(signals, "_win32", lambda: fake)
    signals.init_com_apartment()
    assert calls == [(None, 0x2)]             # COINIT_APARTMENTTHREADED


def test_com_initialisation_failure_is_swallowed(monkeypatch, no_win32):
    monkeypatch.setattr(signals, "IS_WINDOWS", True)
    assert signals.init_com_apartment() is None
