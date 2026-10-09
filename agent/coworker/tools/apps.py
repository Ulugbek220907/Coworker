"""Application and window tools: open a program by name; focus, resize or close a window.

Only open_app starts a program. It runs without a tap when the name is exactly one
entry of the Start Menu index (launcher.resolve_exact). Any other name, and any turn
that is not the owner's own, is confirmed first.

window_close asks a window to close, which can lose unsaved work, so it is
DESTRUCTIVE and never relaxed. Window titles are metadata (what the owner sees),
not content, so these results do not mark the turn untrusted.
"""
from __future__ import annotations

from .. import launcher, uia
from ..core.types import CallContext, Tier, ToolResult
from ..system import WindowVisualState
from .registry import ToolCall, ToolSpec

_HANDLE = {"type": "integer", "minimum": 1, "description": "window handle from list_windows"}
_STATES = {
    "minimize": WindowVisualState.MINIMIZED,
    "maximize": WindowVisualState.MAXIMIZED,
    "normal": WindowVisualState.NORMAL,
}
_WINDOW_ERROR = "Oyna bilan ishlab bo'lmadi."


def _open_app(call: ToolCall) -> ToolResult:
    result = launcher.launch(str(call.args.get("name", "")))
    if "error" in result:
        return ToolResult.fail(result.get("code", "arg_invalid"), result["error"])
    if result.get("ambiguous"):
        return ToolResult.fail(
            "arg_invalid",
            "Bir nechta dastur mos keldi; qaysi birini ochish kerakligini ayting.",
            options=result["options"],
        )
    return ToolResult(ok=True, data={"opened": result["opened"]})


def _relax_open_app(args: dict, _ctx: CallContext) -> bool:
    """Skip the tap only for an exact indexed shortcut name; fuzzy names keep the confirmation."""
    return launcher.resolve_exact(str(args.get("name", ""))) is not None


def _window_focus(call: ToolCall) -> ToolResult:
    res = uia.focus_window(int(call.args["handle"]))
    if not res.get("ok"):
        return ToolResult(ok=False, error=res.get("error", _WINDOW_ERROR))
    return ToolResult(ok=True, data={"window": res.get("window", ""), "foreground": True})


def _window_state(call: ToolCall) -> ToolResult:
    state = str(call.args["state"])
    res = uia.set_window_state(int(call.args["handle"]), _STATES[state])
    if not res.get("ok"):
        return ToolResult(ok=False, error=res.get("error", _WINDOW_ERROR))
    return ToolResult(ok=True, data={"window": res.get("window", ""), "state": state})


def _window_close(call: ToolCall) -> ToolResult:
    res = uia.close_window(int(call.args["handle"]))
    if not res.get("ok"):
        return ToolResult(ok=False, error=res.get("error", _WINDOW_ERROR))
    # A close request, not a confirmed close: the window may still ask to save.
    return ToolResult(ok=True, data={"close_requested": True, "window": res.get("closed", "")})


def _summary_open(args: dict) -> str:
    return f"Dasturni ochish: {args.get('name', '')}"


def _summary_close(args: dict) -> str:
    return f"Oynani yopish: handle {args.get('handle')}. Saqlanmagan ish yo'qolishi mumkin."


SPECS: list[ToolSpec] = [
    ToolSpec(
        name="open_app",
        family="apps",
        tier=Tier.SYSTEM_CHANGE,
        description=(
            "Open an installed application by name, from the Start Menu or desktop, "
            "or a built-in Windows app such as notepad or calculator. Shells, "
            "interpreters and script hosts are refused."
        ),
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "maxLength": 120}},
            "required": ["name"],
        },
        handler=_open_app,
        gov_class="TOOL",
        timeout_s=20.0,
        sensitive_args=("name",),
        relax=_relax_open_app,
        summary=_summary_open,
    ),
    ToolSpec(
        name="window_focus",
        family="apps",
        tier=Tier.READ,
        description="Bring a window to the front by its handle from list_windows.",
        parameters={"type": "object", "properties": {"handle": _HANDLE}, "required": ["handle"]},
        handler=_window_focus,
        gov_class="UIA",
        timeout_s=30.0,
    ),
    ToolSpec(
        name="window_state",
        family="apps",
        tier=Tier.READ,
        description="Minimize, maximize or restore a window by its handle from list_windows.",
        parameters={
            "type": "object",
            "properties": {
                "handle": _HANDLE,
                "state": {"type": "string", "enum": list(_STATES)},
            },
            "required": ["handle", "state"],
        },
        handler=_window_state,
        gov_class="UIA",
        timeout_s=30.0,
    ),
    ToolSpec(
        name="window_close",
        family="apps",
        tier=Tier.DESTRUCTIVE,
        description=(
            "Ask a window to close gracefully. The window may still prompt to save; the "
            "result reports close_requested, not a confirmed close."
        ),
        parameters={"type": "object", "properties": {"handle": _HANDLE}, "required": ["handle"]},
        handler=_window_close,
        gov_class="UIA",
        timeout_s=30.0,
        reversible=False,
        summary=_summary_close,
    ),
]
