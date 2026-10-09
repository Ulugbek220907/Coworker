"""System tools: tier table, validated relax hooks, schema rejections and handler results.

Every call that would change the machine (audio, lock, sleep, shutdown) is replaced
by a fake, and an autouse guard makes an unpatched real call fail the test.
"""
from __future__ import annotations

import sys

import pytest

from coworker import system as sysops
from coworker.core.ports import PowerState
from coworker.core.types import Autonomy, CallContext, Provenance, Tier
from coworker.tools import system as tools
from coworker.tools.registry import Registry, Services, ToolCall, validate_args

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="system tools target Windows")

# Section 3 of docs/architecture-v2.md, the system rows.
SECTION_3_TIERS = {
    "volume_get": Tier.READ,
    "volume_set": Tier.SYSTEM_CHANGE,
    "volume_adjust": Tier.SYSTEM_CHANGE,
    "volume_mute": Tier.SYSTEM_CHANGE,
    "system_status": Tier.READ,
    "processes_list": Tier.READ,
    "lock_screen": Tier.SYSTEM_CHANGE,
    "power_action": Tier.SYSTEM_CHANGE,
}


@pytest.fixture(autouse=True)
def _no_real_machine_changes(monkeypatch):
    def forbidden(name):
        def boom(*args, **kwargs):
            raise AssertionError(f"real system call {name} from a test")
        return boom

    for name in ("lock_workstation", "_suspend_system", "_run_shutdown", "get_volume",
                 "set_volume", "adjust_volume", "set_mute"):
        monkeypatch.setattr(sysops, name, forbidden(name))


def _spec(name: str):
    return next(s for s in tools.SPECS if s.name == name)


def _ctx(provenance: Provenance = Provenance.OWNER) -> CallContext:
    return CallContext(
        turn_id="t1", actor="owner", chat_id=1, autonomy=Autonomy.ASK_FOR_WRITES,
        grants=frozenset({"system"}), generation=0, provenance=provenance,
    )


def _call(name: str, args: dict, svc: Services | None = None) -> ToolCall:
    return ToolCall(name=name, args=args, ctx=_ctx(), svc=svc or Services())


class FakeOs:
    """An OsPort that records which signals were asked for."""

    def __init__(self, percent=None, plugged=None, saver=False) -> None:
        self._power = PowerState(percent, plugged, saver)
        self.asked: set[str] = set()

    def idle_seconds(self) -> float:
        return 0.0

    def cpu_percent(self) -> float:
        self.asked.add("cpu")
        return 12.0

    def free_ram_mb(self) -> float:
        self.asked.add("ram")
        return 2048.0

    def power(self) -> PowerState:
        self.asked.add("power")
        return self._power

    def is_elevated(self) -> bool:
        return False

    def input_desktop_available(self) -> bool:
        return True


# ----------------------------------------------------------------- registry

def test_specs_register_and_carry_the_section_3_tiers():
    reg = Registry()
    reg.register_many(tools.SPECS)
    assert {s.name for s in reg.all()} == set(SECTION_3_TIERS)
    for name, tier in SECTION_3_TIERS.items():
        spec = reg.get(name)
        assert spec.family == "system"
        assert spec.tier == tier


def test_lock_and_power_are_never_relaxed():
    assert _spec("lock_screen").relax is None
    assert _spec("power_action").relax is None


def test_reads_have_no_relax_hook():
    for name in ("volume_get", "system_status", "processes_list"):
        assert _spec(name).relax is None


# -------------------------------------------------------- relax (owner turns)

@pytest.mark.parametrize("name, args, expected", [
    ("volume_set", {"percent": 40}, True),
    ("volume_set", {"percent": 0}, True),
    ("volume_set", {"percent": 100.0}, True),
    ("volume_set", {"percent": 101}, False),
    ("volume_set", {"percent": -1}, False),
    ("volume_set", {"percent": float("nan")}, False),
    ("volume_set", {"percent": True}, False),
    ("volume_set", {}, False),
    ("volume_adjust", {"delta": -100}, True),
    ("volume_adjust", {"delta": 25.5}, True),
    ("volume_adjust", {"delta": 100.5}, False),
    ("volume_adjust", {"delta": float("inf")}, False),
    ("volume_adjust", {"delta": "10"}, False),
    ("volume_mute", {"mute": True}, True),
    ("volume_mute", {"mute": False}, True),
    ("volume_mute", {"mute": 1}, False),
    ("volume_mute", {}, False),
])
def test_volume_relax_follows_the_validated_ranges(name, args, expected):
    assert _spec(name).relax(args, _ctx()) is expected


# ------------------------------------------------------ schema (what the model sees)

@pytest.mark.parametrize("name, args, fragment", [
    ("volume_set", {"percent": 150}, "at most 100"),
    ("volume_set", {"percent": -0.5}, "at least 0"),
    ("volume_set", {"percent": float("nan")}, "must be finite"),
    ("volume_set", {"percent": "50"}, "must be a number"),
    ("volume_set", {"percent": True}, "must be a number"),
    ("volume_adjust", {"delta": -101}, "at least -100"),
    ("volume_adjust", {"delta": 101}, "at most 100"),
    ("volume_adjust", {"delta": float("inf")}, "must be finite"),
    ("volume_mute", {"mute": "yes"}, "must be true or false"),
    ("volume_get", {"extra": 1}, "unexpected argument: extra"),
    ("power_action", {"action": "reboot"}, "must be one of sleep, restart, shutdown"),
    ("power_action", {}, "missing argument: action"),
    ("processes_list", {"limit": 0}, "at least 1"),
    ("processes_list", {"limit": 500}, "at most 200"),
])
def test_schema_rejects_bad_arguments(name, args, fragment):
    err = validate_args(_spec(name).parameters, args)
    assert err is not None and fragment in err


@pytest.mark.parametrize("name, args", [
    ("volume_set", {"percent": 0}),
    ("volume_set", {"percent": 100}),
    ("volume_adjust", {"delta": -100}),
    ("volume_adjust", {"delta": 100}),
    ("volume_mute", {"mute": False}),
    ("power_action", {"action": "sleep"}),
    ("processes_list", {"limit": 200}),
])
def test_schema_accepts_the_boundary_values(name, args):
    assert validate_args(_spec(name).parameters, args) is None


# ------------------------------------------------------------ volume handlers

def test_volume_get_returns_level_and_mute(monkeypatch):
    monkeypatch.setattr(sysops, "get_volume", lambda: {"percent": 35, "muted": False})
    result = _spec("volume_get").handler(_call("volume_get", {}))
    assert result.ok is True
    assert result.data == {"percent": 35, "muted": False}


def test_volume_set_passes_the_value_through(monkeypatch):
    seen: list = []
    monkeypatch.setattr(sysops, "set_volume", lambda p: seen.append(p) or {"ok": True, "percent": 40})
    result = _spec("volume_set").handler(_call("volume_set", {"percent": 40}))
    assert result.ok is True and result.data == {"percent": 40}
    assert seen == [40]


def test_volume_set_reports_a_device_error(monkeypatch):
    monkeypatch.setattr(sysops, "set_volume", lambda p: {"error": "Ovozni o'zgartirib bo'lmadi"})
    result = _spec("volume_set").handler(_call("volume_set", {"percent": 40}))
    assert result.ok is False


def test_volume_adjust_reports_the_change(monkeypatch):
    monkeypatch.setattr(sysops, "adjust_volume", lambda d: {"ok": True, "from": 30, "percent": 40})
    result = _spec("volume_adjust").handler(_call("volume_adjust", {"delta": 10}))
    assert result.data == {"from": 30, "percent": 40}


def test_volume_mute_reports_the_state(monkeypatch):
    monkeypatch.setattr(sysops, "set_mute", lambda m: {"ok": True, "muted": m})
    result = _spec("volume_mute").handler(_call("volume_mute", {"mute": True}))
    assert result.data == {"muted": True}


@pytest.mark.parametrize("name, args, text", [
    ("volume_set", {"percent": 40}, "40% ga"),
    ("volume_adjust", {"delta": -15}, "-15"),
    ("volume_mute", {"mute": True}, "o'chirish"),
    ("volume_mute", {"mute": False}, "yoqish"),
])
def test_volume_summaries_name_the_change(name, args, text):
    assert text in _spec(name).summary(args)


# ------------------------------------------------------------- system status

def test_system_status_reads_only_through_the_os_port():
    port = FakeOs(percent=76, plugged=False, saver=True)
    result = _spec("system_status").handler(_call("system_status", {}, Services(os=port)))
    assert result.ok is True
    assert result.data["cpu_percent"] == 12
    assert result.data["ram_free_mb"] == 2048
    assert result.data["battery"] == {"percent": 76, "plugged": False, "saver": True}
    assert port.asked == {"cpu", "ram", "power"}


def test_system_status_without_an_os_port_is_not_configured():
    result = _spec("system_status").handler(_call("system_status", {}, Services()))
    assert result.ok is False
    assert result.code == "not_configured"


# ------------------------------------------------------------- process names

def test_processes_list_never_returns_command_lines(monkeypatch):
    rows = [
        {"pid": 40, "name": "Telegram.exe", "cmdline": "Telegram.exe --token=SECRET"},
        {"pid": 7, "name": "cmd.exe", "cmdline": "cmd.exe /c echo SECRET"},
        {"pid": 12, "name": "explorer.exe", "cmdline": "explorer.exe"},
    ]
    monkeypatch.setattr(sysops, "_enumerate_processes", lambda: rows)
    result = _spec("processes_list").handler(_call("processes_list", {"limit": 2}))
    assert result.ok is True
    assert result.data["total"] == 3
    assert [p["name"] for p in result.data["processes"]] == ["cmd.exe", "explorer.exe"]
    for proc in result.data["processes"]:
        assert set(proc) == {"pid", "name"}
    assert "SECRET" not in repr(result.data)


def test_processes_list_defaults_to_fifty(monkeypatch):
    rows = [{"pid": i, "name": f"p{i:03}.exe"} for i in range(80)]
    monkeypatch.setattr(sysops, "_enumerate_processes", lambda: rows)
    result = _spec("processes_list").handler(_call("processes_list", {}))
    assert len(result.data["processes"]) == 50
    assert result.data["total"] == 80


# ------------------------------------------------------------- lock and power

def test_lock_screen_locks_the_session(monkeypatch):
    calls: list[int] = []

    def fake():
        calls.append(1)
        return {"ok": True}

    monkeypatch.setattr(sysops, "lock_workstation", fake)
    result = _spec("lock_screen").handler(_call("lock_screen", {}))
    assert result.ok is True and result.data == {"locked": True}
    assert calls == [1]


def test_lock_screen_reports_a_failed_lock(monkeypatch):
    monkeypatch.setattr(sysops, "lock_workstation", lambda: {"error": "Ekranni qulflab bo'lmadi."})
    assert _spec("lock_screen").handler(_call("lock_screen", {})).ok is False


@pytest.mark.parametrize("action, label", [
    ("sleep", "uyquga o'tkazish"), ("restart", "qayta yuklash"), ("shutdown", "o'chirish"),
])
def test_power_summaries_name_the_action(action, label):
    assert label in _spec("power_action").summary({"action": action})


def test_power_action_runs_the_named_call(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(sysops, "_suspend_system", lambda: seen.append("sleep") or True)
    monkeypatch.setattr(sysops, "_run_shutdown", lambda flag: seen.append(flag) or True)
    result = _spec("power_action").handler(_call("power_action", {"action": "restart"}))
    assert result.ok is True and result.data == {"action": "restart"}
    assert seen == ["/r"]


def test_power_action_reports_a_failed_call(monkeypatch):
    monkeypatch.setattr(sysops, "_run_shutdown", lambda flag: False)
    result = _spec("power_action").handler(_call("power_action", {"action": "shutdown"}))
    assert result.ok is False
