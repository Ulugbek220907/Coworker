"""System module: value validation, audio timeout, power command shape and process projection.

The Core Audio worker, the power calls and subprocess.run are all replaced, so
no volume changes, no sleep and no shutdown can happen from these tests.
"""
from __future__ import annotations

import os
import sys

import pytest

from coworker import system, uia
from coworker.core.ports import PowerState

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="system calls are Windows-only")

NAN = float("nan")
INF = float("inf")


class _StuckWorker:
    """Stands in for the UIA/COM thread: records the timeout, then never answers."""

    def __init__(self) -> None:
        self.timeouts: list[float] = []
        self.jobs = 0

    def call(self, fn, timeout=None):
        self.timeouts.append(timeout)
        self.jobs += 1
        raise TimeoutError("UIA javob bermadi")


@pytest.fixture
def audio(monkeypatch) -> _StuckWorker:
    worker = _StuckWorker()
    monkeypatch.setattr(uia, "_worker", worker)
    monkeypatch.setattr(system, "available", lambda: True)
    return worker


# ------------------------------------------------------------------ validation

@pytest.mark.parametrize("value, expected", [
    (0, 0.0), (100, 100.0), (0.0, 0.0), (50.5, 50.5), (100.0, 100.0), (0.001, 0.001),
])
def test_percent_accepts_finite_values_in_range(value, expected):
    assert system.validate_percent(value) == expected


@pytest.mark.parametrize("value", [
    -0.01, 100.01, -1, 101, NAN, INF, -INF, True, False, "50", None, [50], 10 ** 400,
])
def test_percent_refuses_everything_else(value):
    with pytest.raises(ValueError):
        system.validate_percent(value)


@pytest.mark.parametrize("value, expected", [
    (-100, -100.0), (100, 100.0), (0, 0.0), (-0.5, -0.5), (99.9, 99.9),
])
def test_delta_accepts_finite_values_in_range(value, expected):
    assert system.validate_delta(value) == expected


@pytest.mark.parametrize("value", [
    -100.01, 100.01, NAN, INF, -INF, True, "10", None, 10 ** 400,
])
def test_delta_refuses_everything_else(value):
    with pytest.raises(ValueError):
        system.validate_delta(value)


@pytest.mark.parametrize("value", [150, -5, NAN, "40", True])
def test_set_volume_refuses_before_touching_audio(audio, value):
    result = system.set_volume(value)
    assert "error" in result
    assert audio.jobs == 0


@pytest.mark.parametrize("value", [150, -150, NAN, INF])
def test_adjust_volume_refuses_before_touching_audio(audio, value):
    result = system.adjust_volume(value)
    assert "error" in result
    assert audio.jobs == 0


# ----------------------------------------------------------------- audio timeout

def test_audio_call_runs_with_the_named_timeout(audio):
    result = system.get_volume()
    assert "error" in result and "UIA" in result["error"]
    assert audio.timeouts == [system.AUDIO_TIMEOUT_S]


def test_every_audio_write_is_bounded_by_the_same_timeout(audio):
    system.set_volume(30)
    system.adjust_volume(-10)
    system.set_mute(True)
    assert audio.timeouts == [system.AUDIO_TIMEOUT_S] * 3


def test_audio_timeout_is_a_positive_bound():
    assert 0 < system.AUDIO_TIMEOUT_S <= 30


# ----------------------------------------------------------------- window states

def test_window_visual_states_match_uia_values():
    assert system.WindowVisualState.NORMAL == 0
    assert system.WindowVisualState.MAXIMIZED == 1
    assert system.WindowVisualState.MINIMIZED == 2


# --------------------------------------------------------------------- power

def _record_power(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(system, "_suspend_system", lambda: seen.append("suspend") or True)
    monkeypatch.setattr(system, "_run_shutdown", lambda flag: seen.append(flag) or True)
    return seen


def test_power_refuses_an_action_outside_the_enum(monkeypatch):
    seen = _record_power(monkeypatch)
    assert "error" in system.power_action("reboot")
    assert "error" in system.power_action("hibernate")
    assert seen == []


@pytest.mark.parametrize("action, expected", [
    ("sleep", ["suspend"]), ("restart", ["/r"]), ("shutdown", ["/s"]),
])
def test_power_routes_each_action_to_its_call(monkeypatch, action, expected):
    seen = _record_power(monkeypatch)
    assert system.power_action(action) == {"ok": True, "action": action}
    assert seen == expected


def test_power_reports_a_failed_call(monkeypatch):
    monkeypatch.setattr(system, "_run_shutdown", lambda flag: False)
    assert "error" in system.power_action("shutdown")


def test_shutdown_runs_the_system_binary_by_absolute_path_without_a_shell(monkeypatch):
    calls: list[tuple] = []

    class _Done:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _Done()

    monkeypatch.setattr(system.subprocess, "run", fake_run)
    assert system._run_shutdown("/r") is True
    argv, kwargs = calls[0]
    assert os.path.isabs(argv[0]) and argv[0].lower().endswith("shutdown.exe")
    assert argv[1:] == ["/r", "/t", "0"]
    assert kwargs["shell"] is False
    assert "/f" not in argv


# ------------------------------------------------------------------ processes

def test_process_projection_keeps_only_name_and_pid(monkeypatch):
    rows = [
        {"pid": 40, "name": "Telegram.exe", "cmdline": "Telegram.exe --token=SECRET"},
        {"pid": 7, "name": "cmd.exe", "cmdline": "cmd.exe /c echo SECRET"},
        {"pid": 12, "name": "explorer.exe", "cmdline": "explorer.exe"},
    ]
    monkeypatch.setattr(system, "_enumerate_processes", lambda: rows)
    result = system.list_processes(limit=10)
    assert result["total"] == 3
    assert [p["name"] for p in result["processes"]] == ["cmd.exe", "explorer.exe", "Telegram.exe"]
    for proc in result["processes"]:
        assert set(proc) == {"pid", "name"}
    assert "SECRET" not in repr(result)


def test_process_limit_caps_the_list_but_not_the_total(monkeypatch):
    rows = [{"pid": i, "name": f"p{i}.exe"} for i in range(10)]
    monkeypatch.setattr(system, "_enumerate_processes", lambda: rows)
    result = system.list_processes(limit=3)
    assert len(result["processes"]) == 3
    assert result["total"] == 10


@pytest.mark.windows
def test_real_process_list_names_this_process():
    rows = system._enumerate_processes()
    mine = next(r for r in rows if r["pid"] == os.getpid())
    assert mine["name"].lower().endswith(".exe")
    assert set(mine) == {"pid", "name"}


# ------------------------------------------------------------------ snapshot

class _FakeOs:
    def __init__(self, percent, plugged=None, saver=False) -> None:
        self._power = PowerState(percent, plugged, saver)

    def idle_seconds(self) -> float:
        return 0.0

    def cpu_percent(self) -> float:
        return 12.6

    def free_ram_mb(self) -> float:
        return 2048.4

    def power(self) -> PowerState:
        return self._power

    def is_elevated(self) -> bool:
        return False

    def input_desktop_available(self) -> bool:
        return True


@pytest.mark.windows
def test_snapshot_reads_cpu_memory_and_battery_from_the_port():
    snap = system.system_snapshot(_FakeOs(percent=76, plugged=False, saver=True))
    assert snap["cpu_percent"] == 13
    assert snap["ram_free_mb"] == 2048
    assert snap["battery"] == {"percent": 76, "plugged": False, "saver": True}
    assert set(snap["disk"]) == {"drive", "total_gb", "free_gb", "used_percent"}
    assert isinstance(snap["uptime_s"], int) and snap["uptime_s"] >= 0


@pytest.mark.windows
def test_snapshot_has_no_battery_on_a_desktop():
    assert system.system_snapshot(_FakeOs(percent=None))["battery"] is None
