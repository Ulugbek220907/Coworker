"""System tools: volume, status, process names, lock and power.

Volume is reversible and benign, so an owner's own turn may change it without a
tap, but only with a value inside the validated range. Lock and power are never
relaxed: they end the session or interrupt unsaved work, so every call is confirmed.

Process names are the only process data offered. The tool never returns command
lines, which can carry tokens or other arguments.
"""
from __future__ import annotations

from .. import system as sysops
from ..core.types import CallContext, Tier, ToolResult
from .registry import ToolCall, ToolSpec

_PROCESS_LIMIT_MAX = 200


def _valid(check, value) -> bool:
    try:
        check(value)
    except ValueError:
        return False
    return True


def _relax_percent(args: dict, _ctx: CallContext) -> bool:
    return _valid(sysops.validate_percent, args.get("percent"))


def _relax_delta(args: dict, _ctx: CallContext) -> bool:
    return _valid(sysops.validate_delta, args.get("delta"))


def _relax_mute(args: dict, _ctx: CallContext) -> bool:
    return isinstance(args.get("mute"), bool)


def _failed(res: dict) -> ToolResult:
    return ToolResult(ok=False, error=res["error"])


def _volume_get(call: ToolCall) -> ToolResult:
    res = sysops.get_volume()
    if "error" in res:
        return _failed(res)
    return ToolResult(ok=True, data={"percent": res["percent"], "muted": res["muted"]})


def _volume_set(call: ToolCall) -> ToolResult:
    res = sysops.set_volume(call.args["percent"])
    if "error" in res:
        return _failed(res)
    return ToolResult(ok=True, data={"percent": res["percent"]})


def _volume_adjust(call: ToolCall) -> ToolResult:
    res = sysops.adjust_volume(call.args["delta"])
    if "error" in res:
        return _failed(res)
    return ToolResult(ok=True, data={"from": res["from"], "percent": res["percent"]})


def _volume_mute(call: ToolCall) -> ToolResult:
    res = sysops.set_mute(bool(call.args["mute"]))
    if "error" in res:
        return _failed(res)
    return ToolResult(ok=True, data={"muted": res["muted"]})


def _system_status(call: ToolCall) -> ToolResult:
    port = call.svc.os if call.svc is not None else None
    if port is None:
        return ToolResult.fail("not_configured", "Tizim holatini o'qish uchun OS ulanmagan.")
    return ToolResult(ok=True, data=sysops.system_snapshot(port))


def _processes_list(call: ToolCall) -> ToolResult:
    limit = int(call.args.get("limit", 50))
    return ToolResult(ok=True, data=sysops.list_processes(limit))


def _lock_screen(call: ToolCall) -> ToolResult:
    res = sysops.lock_workstation()
    if "error" in res:
        return _failed(res)
    return ToolResult(ok=True, data={"locked": True})


def _power_action(call: ToolCall) -> ToolResult:
    action = str(call.args["action"])
    res = sysops.power_action(action)
    if "error" in res:
        return _failed(res)
    return ToolResult(ok=True, data={"action": action})


def _summary_percent(args: dict) -> str:
    return f"Ovoz balandligini {args.get('percent')}% ga qo'yish"


def _summary_delta(args: dict) -> str:
    return f"Ovozni {args.get('delta')} foizga o'zgartirish"


def _summary_mute(args: dict) -> str:
    return "Ovozni o'chirish" if args.get("mute") else "Ovozni yoqish"


def _summary_power(args: dict) -> str:
    label = sysops.POWER_LABELS.get(str(args.get("action")), str(args.get("action")))
    return f"Kompyuterni {label}"


_NO_ARGS = {"type": "object", "properties": {}}

SPECS: list[ToolSpec] = [
    ToolSpec(
        name="volume_get",
        family="system",
        tier=Tier.READ,
        description="Read the master speaker volume (percent) and whether it is muted.",
        parameters=_NO_ARGS,
        handler=_volume_get,
        gov_class="TOOL",
        timeout_s=15.0,
    ),
    ToolSpec(
        name="volume_set",
        family="system",
        tier=Tier.SYSTEM_CHANGE,
        description="Set the master speaker volume to an absolute percentage from 0 to 100.",
        parameters={
            "type": "object",
            "properties": {"percent": {"type": "number", "minimum": 0, "maximum": 100}},
            "required": ["percent"],
        },
        handler=_volume_set,
        gov_class="TOOL",
        timeout_s=15.0,
        relax=_relax_percent,
        summary=_summary_percent,
    ),
    ToolSpec(
        name="volume_adjust",
        family="system",
        tier=Tier.SYSTEM_CHANGE,
        description="Change the master volume by a relative number of percentage points, from -100 to 100.",
        parameters={
            "type": "object",
            "properties": {"delta": {"type": "number", "minimum": -100, "maximum": 100}},
            "required": ["delta"],
        },
        handler=_volume_adjust,
        gov_class="TOOL",
        timeout_s=15.0,
        relax=_relax_delta,
        summary=_summary_delta,
    ),
    ToolSpec(
        name="volume_mute",
        family="system",
        tier=Tier.SYSTEM_CHANGE,
        description="Mute (true) or unmute (false) the master speaker volume.",
        parameters={
            "type": "object",
            "properties": {"mute": {"type": "boolean"}},
            "required": ["mute"],
        },
        handler=_volume_mute,
        gov_class="TOOL",
        timeout_s=15.0,
        relax=_relax_mute,
        summary=_summary_mute,
    ),
    ToolSpec(
        name="system_status",
        family="system",
        tier=Tier.READ,
        description="Report CPU load, free memory, battery, system drive space and uptime.",
        parameters=_NO_ARGS,
        handler=_system_status,
        gov_class="TOOL",
        timeout_s=15.0,
    ),
    ToolSpec(
        name="processes_list",
        family="system",
        tier=Tier.READ,
        description="List running process names and ids, sorted by name. Command lines are not available.",
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": _PROCESS_LIMIT_MAX}},
        },
        handler=_processes_list,
        gov_class="TOOL",
        timeout_s=15.0,
    ),
    ToolSpec(
        name="lock_screen",
        family="system",
        tier=Tier.SYSTEM_CHANGE,
        description="Lock the Windows session. Always asks the owner first.",
        parameters=_NO_ARGS,
        handler=_lock_screen,
        gov_class="TOOL",
        timeout_s=15.0,
        summary=lambda _args: "Ekranni qulflash",
    ),
    ToolSpec(
        name="power_action",
        family="system",
        tier=Tier.SYSTEM_CHANGE,
        description=(
            "Put the computer to sleep, restart it, or shut it down. Always asks the owner first; "
            "running apps can still block a restart or shutdown."
        ),
        parameters={
            "type": "object",
            "properties": {"action": {"type": "string", "enum": list(sysops.POWER_ACTIONS)}},
            "required": ["action"],
        },
        handler=_power_action,
        gov_class="TOOL",
        timeout_s=30.0,
        sensitive_args=("action",),
        summary=_summary_power,
    ),
]
