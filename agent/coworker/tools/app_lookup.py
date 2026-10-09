"""Read-only lookups that let the model name an application before it opens one.

The model used to guess a program name and hand it to open_app, and a guessed name
could start the wrong program. These tools answer the question first: find_app
reports what a name resolves to, installed_browsers reports which browsers the
Start Menu holds under their own names, and default_browser reports the browser the
owner's Windows opens web links with. The name that open_app then receives is one a
lookup returned.

None of these starts anything. Their results are program names and a registry
value, which are metadata rather than content, so the turn is not marked untrusted.
"""
from __future__ import annotations

from .. import launcher
from ..core.types import Tier, ToolResult
from .registry import ToolCall, ToolSpec

try:
    import winreg
except ImportError:  # not Windows: default_browser reports that it does not know
    winreg = None

# The browsers the owner can name, by the display name the Start Menu shows them under.
BROWSERS = ("Google Chrome", "Microsoft Edge", "Firefox", "Brave", "Opera")

# Where Windows records the owner's chosen handler for web links. The ProgId value is
# Windows' own identifier for that handler, so it is matched by prefix and never by
# the owner's words.
_USER_CHOICE = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
_PROG_ID_FAMILIES = (
    ("ChromeHTML", "Google Chrome"),
    ("MSEdgeHTM", "Microsoft Edge"),
    ("FirefoxURL", "Firefox"),
    ("BraveHTML", "Brave"),
    ("OperaStable", "Opera"),
)


def find_app(query: str) -> dict:
    """What a spoken application name resolves to: status, the exact name, and options to choose from."""
    res = launcher.resolve(query)
    return {"status": res["status"], "name": res["name"], "options": res["options"]}


def installed_browsers() -> dict:
    """The browsers in BROWSERS that resolve exactly, so a partial match never counts.

    "Chrome Remote Desktop" must not be reported as Google Chrome, and "Firefox
    Developer Edition" must not be reported as Firefox.
    """
    installed = [name for name in BROWSERS if launcher.resolve(name)["status"] == "exact"]
    return {"installed": installed}


def _user_choice_prog_id() -> str | None:
    """The ProgId of the owner's https handler, read from the current user's registry hive."""
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _USER_CHOICE) as key:
            value, _kind = winreg.QueryValueEx(key, "ProgId")
    except OSError:
        return None
    text = str(value or "").strip()
    return text or None


def default_browser() -> dict:
    """The browser the owner's Windows opens web links with, by display name when it is known."""
    prog_id = _user_choice_prog_id()
    name = None
    if prog_id:
        lowered = prog_id.lower()
        name = next(
            (display for prefix, display in _PROG_ID_FAMILIES if lowered.startswith(prefix.lower())),
            None,
        )
    return {"name": name, "prog_id": prog_id}


def _find_app(call: ToolCall) -> ToolResult:
    return ToolResult(ok=True, data=find_app(str(call.args.get("query", ""))))


def _installed_browsers(_call: ToolCall) -> ToolResult:
    return ToolResult(ok=True, data=installed_browsers())


def _default_browser(_call: ToolCall) -> ToolResult:
    return ToolResult(ok=True, data=default_browser())


SPECS: list[ToolSpec] = [
    ToolSpec(
        name="find_app",
        family="apps",
        tier=Tier.READ,
        description=(
            "Check which installed application a name means, before opening it. Status 'exact' "
            "gives the name to pass to open_app; 'choose' gives up to four options to ask the "
            "owner about; 'none' means nothing matches. Read-only: it starts nothing."
        ),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "maxLength": 120}},
            "required": ["query"],
        },
        handler=_find_app,
        gov_class="TOOL",
        timeout_s=10.0,
    ),
    ToolSpec(
        name="installed_browsers",
        family="apps",
        tier=Tier.READ,
        description=(
            "List which of Google Chrome, Microsoft Edge, Firefox, Brave and Opera are installed "
            "under exactly that name. Read-only."
        ),
        parameters={"type": "object", "properties": {}},
        handler=_installed_browsers,
        gov_class="TOOL",
        timeout_s=10.0,
    ),
    ToolSpec(
        name="default_browser",
        family="apps",
        tier=Tier.READ,
        description=(
            "Find the browser the owner's Windows opens web links with. Returns its display name "
            "when it is known, which can be passed to open_app. Read-only."
        ),
        parameters={"type": "object", "properties": {}},
        handler=_default_browser,
        gov_class="TOOL",
        timeout_s=10.0,
    ),
]
