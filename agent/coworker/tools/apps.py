"""Application and window tools: open a program by name; focus, resize or close a window.

Only open_app starts a program. A name that does not resolve to exactly one installed
shortcut is refused before any card is shown (_check_open_app): a partial name gets
need_choice with its options, an unknown name gets not_found. An exact name runs
without a tap for the owner's own turn (launcher.resolve); any other turn is confirmed.

window_close asks a window to close, which can lose unsaved work, so it is
DESTRUCTIVE and never relaxed. Window titles are metadata (what the owner sees),
not content, so these results do not mark the turn untrusted.
"""
from __future__ import annotations

import logging
import os
import subprocess

from .. import launcher, uia
from ..core.types import CallContext, Tier, ToolResult, Verdict
from ..system import WindowVisualState
from .registry import ToolCall, ToolSpec

log = logging.getLogger("apps")

_HANDLE = {"type": "integer", "minimum": 1, "description": "window handle from list_windows"}
_STATES = {
    "minimize": WindowVisualState.MINIMIZED,
    "maximize": WindowVisualState.MAXIMIZED,
    "normal": WindowVisualState.NORMAL,
}
_WINDOW_ERROR = "Oyna bilan ishlab bo'lmadi."
_REFUSAL_REASON = {
    "arg_invalid": "the application name is empty",
    "hard_deny": "the name is a shell, an interpreter or a script host, which is never opened",
}


def _open_app(call: ToolCall) -> ToolResult:
    result = launcher.launch(str(call.args.get("name", "")))
    if "error" in result:
        return ToolResult.fail(result.get("code", "arg_invalid"), result["error"])
    if result.get("ambiguous"):
        return ToolResult.fail(
            "need_choice",
            "Bir nechta dastur mos keldi; qaysi birini ochish kerakligini ayting.",
            options=result["options"],
        )
    return ToolResult(ok=True, data={"opened": result["opened"]})


def _check_open_app(args: dict, _ctx: CallContext, _svc) -> Verdict | None:
    """Refuse an open_app name that does not resolve to one installed shortcut, before any card.

    The check runs ahead of the approval card, so the owner is never asked to approve a
    name that could not be opened, and a model that guesses a name is told to look it up.
    Only an exact resolution passes; the card then names the resolved program.
    """
    name = str(args.get("name", ""))
    res = launcher.resolve(name)
    status = res["status"]
    if status == "exact":
        return None
    if status == "refused":
        code = launcher.refusal_code(name) or "arg_invalid"
        return Verdict.deny(code, _REFUSAL_REASON[code])
    if status == "choose":
        options = ", ".join(res["options"])
        return Verdict.deny(
            "need_choice",
            f"the name matches more than one installed application: {options}. "
            "Ask the owner which one, then call open_app with that exact name.",
        )
    return Verdict.deny(
        "not_found",
        "no installed application has this name. Look it up with find_app or "
        "default_browser; do not guess another name.",
    )


def _relax_open_app(args: dict, _ctx: CallContext) -> bool:
    """Skip the tap only for an exact indexed shortcut name; fuzzy names keep the confirmation."""
    return launcher.resolve(str(args.get("name", "")))["status"] == "exact"


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
    """Name the program the owner will see start, never the raw words of an unresolved name."""
    res = launcher.resolve(str(args.get("name", "")))
    if res["status"] == "exact":
        return f"Dasturni ochish: {res['name']}"
    return "Dasturni ochish"


def _summary_close(args: dict) -> str:
    return f"Oynani yopish: handle {args.get('handle')}. Saqlanmagan ish yo'qolishi mumkin."


def _shortcut_target(lnk: str) -> str | None:
    """The executable a Start Menu shortcut starts, read through Windows' own shortcut reader."""
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        target = win32com.client.Dispatch("WScript.Shell").CreateShortcut(lnk).TargetPath
    except Exception as exc:
        log.warning("shortcut target not read for %s: %s", lnk, exc)
        return None
    return target if target and os.path.isfile(target) else None


def _open_folder_in_app(call: ToolCall) -> ToolResult:
    app = str(call.args.get("app", "")).strip()
    folder = str(call.args.get("folder", ""))
    if not os.path.isdir(folder):
        return ToolResult.fail("path_invalid", "papka topilmadi")
    exact = launcher.resolve_exact(app)
    if exact is None:
        return ToolResult.fail("arg_invalid", f"«{app}» ilovasi Start menyusida aniq topilmadi")
    target = _shortcut_target(exact["path"])
    if target is None:
        return ToolResult.fail("not_configured", f"«{exact['name']}» ilovasining fayli topilmadi")
    try:
        subprocess.Popen([target, folder], shell=False, close_fds=True)
    except OSError as exc:
        return ToolResult.fail("tool_error", f"ochib bo'lmadi: {exc}")
    return ToolResult(ok=True, data={
        "app": exact["name"], "folder": folder,
        "message": f"«{folder}» papkasi «{exact['name']}» ilovasida ochildi.",
    })


SPECS: list[ToolSpec] = [
    ToolSpec(
        name="open_app",
        family="apps",
        tier=Tier.SYSTEM_CHANGE,
        description=(
            "Open an installed application by its full name, from the Start Menu or desktop, "
            "or a built-in Windows app such as notepad or calculator. A partial name or a "
            "category such as \"browser\" is refused: use find_app to check a name, or "
            "default_browser for the browser. Shells, interpreters and script hosts are refused."
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
        arg_checks=(_check_open_app,),
        summary=_summary_open,
    ),
    ToolSpec(
        name="open_folder_in_app",
        family="apps",
        tier=Tier.SYSTEM_CHANGE,
        description=(
            "Open a folder (from find_folder) in an installed application, for example a project "
            "folder in an editor. The application must be one that list_windows or open_app names "
            "exactly; if the name is unclear, ask the owner which application."
        ),
        parameters={
            "type": "object",
            "properties": {
                "app": {"type": "string", "maxLength": 120,
                        "description": "the application's name as in the Start Menu"},
                "folder": {"type": "string", "maxLength": 1024,
                           "description": "the folder path returned by find_folder"},
            },
            "required": ["app", "folder"],
        },
        handler=_open_folder_in_app,
        gov_class="TOOL",
        timeout_s=30.0,
        path_args=("folder",),
        requires_surfaced=("folder",),
        sensitive_args=("folder", "app"),
        summary=lambda args: f"«{args.get('folder', '')}» papkasini «{args.get('app', '')}» ilovasida ochish",
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
