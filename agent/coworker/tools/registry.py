"""Tool declarations, argument validation and schema generation.

A tool is a ``ToolSpec``: a name, a family (the grant that enables it), a risk
tier, a JSON-Schema for its arguments and a handler. Every tool module exports a
module-level ``SPECS`` list; ``load_all()`` collects them. Nothing registers
itself as a side effect of import, so the set of tools is explicit.

The registry never decides whether a call is allowed - that is the policy
kernel's job. It only knows what exists, what the model may see, and how to
validate arguments before anything runs.
"""
from __future__ import annotations

import importlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

from ..core.types import CallContext, Tier, ToolResult, Verdict

if TYPE_CHECKING:  # pragma: no cover
    pass

log = logging.getLogger("tools")

# Grant families. A family is enabled by config; autonomy then decides how each
# tool in it behaves. Adding a family here is a deliberate act.
FAMILIES = frozenset({
    "files", "office", "desktop", "desktop_control", "apps", "system",
    "browser", "web", "shell", "notes", "schedule", "telegram", "mail", "calendar",
})

# Modules that may hold SPECS. Missing optional modules are skipped and logged.
TOOL_MODULES = (
    "coworker.tools.files",
    "coworker.tools.office",
    "coworker.tools.desktop",
    "coworker.tools.apps",
    "coworker.tools.app_lookup",
    "coworker.tools.system",
    "coworker.tools.browser",
    "coworker.tools.web",
    "coworker.tools.shell",
    "coworker.tools.notes",
    "coworker.tools.schedule",
    "coworker.tools.telegram_out",
    "coworker.tools.mail",
    "coworker.tools.calendar",
    "coworker.tools.speech",
)

_NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_MAX_STRING = 20000


@dataclass
class Services:
    """Runtime services handed to every handler. Filled in by runtime.py.

    Attributes are typed loosely on purpose so that modules can be built and
    tested independently. The contract for each attribute is in
    docs/architecture-v2.md.
    """

    store: Any = None        # coworker.store.Store
    governor: Any = None     # coworker.governor.Governor
    budget: Any = None       # coworker.governor.Budget
    index: Any = None        # coworker.fileindex.FileIndex
    outbox: Any = None       # coworker.transport.Outbox: text, document, ask, notify
    approvals: Any = None    # coworker.safety.ApprovalBroker
    llm: Any = None          # coworker.llm_base.ChatProvider
    scheduler: Any = None    # coworker.scheduler.Scheduler
    kill: Any = None         # coworker.safety.KillSwitch
    os: Any = None           # coworker.core.ports.OsPort
    config: Any = None       # coworker.config.Config (read-only use)
    loop: Any = None         # the asyncio loop; sync handlers that need the async model client use it


@dataclass
class ToolCall:
    """Everything a handler needs for one call. Built by the dispatcher."""

    name: str
    args: dict
    ctx: CallContext
    svc: Services

    @property
    def cancel(self):
        return self.ctx.cancel


Handler = Callable[[ToolCall], ToolResult]
ArgCheck = Callable[[dict, CallContext, Optional[Services]], Optional[Verdict]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    family: str
    tier: Tier
    description: str
    parameters: dict
    handler: Handler
    gov_class: str = "NONE"          # governor class the handler runs under
    timeout_s: float = 30.0
    untrusted: bool = False          # output carries content (files, pages, screens)
    internal: bool = False           # writes only Coworker's own store
    self_target: bool = False        # OUTBOUND only to the owner's own chat
    reversible: bool = True
    path_args: tuple = ()            # argument names that hold filesystem paths
    path_write: bool = False         # the paths are written (not only read)
    sensitive_args: tuple = ()       # arguments checked for verbatim-from-content origin
    requires_surfaced: tuple = ()    # arguments that must come from this turn's searches
    relax: Optional[Callable[[dict, CallContext], bool]] = None
    arg_checks: tuple = ()           # extra policy hooks: (args, ctx, svc) -> Verdict | None
    summary: Optional[Callable[[dict], str]] = None
    probe: Optional[Callable[[], bool]] = None
    # The call sends data off the machine (a URL, a query, a body). Its URL and
    # query arguments are origin-checked, and after the turn has read local data
    # it needs the owner's confirmation, or is refused when unattended.
    egress: bool = False
    tags: frozenset = field(default_factory=frozenset)


class RegistryError(ValueError):
    pass


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._probe_cache: dict[str, bool] = {}

    # ------------------------------------------------------------ registration

    def register(self, spec: ToolSpec) -> None:
        if not _NAME.match(spec.name):
            raise RegistryError(f"bad tool name: {spec.name!r}")
        if spec.name in self._tools:
            raise RegistryError(f"duplicate tool: {spec.name}")
        if spec.tier in (Tier.FINANCIAL, Tier.CREDENTIAL):
            raise RegistryError(f"{spec.name}: {spec.tier.value} tools are not allowed")
        if spec.family not in FAMILIES:
            raise RegistryError(f"{spec.name}: unknown family {spec.family!r}")
        if spec.parameters.get("type") != "object":
            raise RegistryError(f"{spec.name}: parameters must be an object schema")
        if spec.internal and spec.tier != Tier.LOCAL_WRITE:
            raise RegistryError(f"{spec.name}: only LOCAL_WRITE tools may be internal")
        if spec.self_target and spec.tier != Tier.OUTBOUND:
            raise RegistryError(f"{spec.name}: self_target only applies to OUTBOUND")
        self._tools[spec.name] = spec

    def register_many(self, specs: list[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    # ----------------------------------------------------------------- lookup

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def _available(self, grants: frozenset) -> list[ToolSpec]:
        out = []
        for spec in self._tools.values():
            if spec.family not in grants:
                continue
            if spec.probe is not None and not self._probe(spec):
                continue
            out.append(spec)
        return out

    def _probe(self, spec: ToolSpec) -> bool:
        if spec.name not in self._probe_cache:
            try:
                self._probe_cache[spec.name] = bool(spec.probe())  # type: ignore[misc]
            except Exception as exc:  # a broken probe hides the tool, it does not crash the agent
                log.warning("probe failed for %s: %s", spec.name, exc)
                self._probe_cache[spec.name] = False
        return self._probe_cache[spec.name]

    def reset_probes(self) -> None:
        self._probe_cache.clear()

    # ---------------------------------------------------------------- schemas

    def openai_tools(self, grants: frozenset) -> list[dict]:
        """Tool list for OpenAI-compatible chat completions."""
        return [
            {"type": "function", "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters,
            }}
            for s in self._available(grants)
        ]

    def anthropic_tools(self, grants: frozenset) -> list[dict]:
        """Tool list for the Anthropic Messages API."""
        return [
            {"name": s.name, "description": s.description, "input_schema": s.parameters}
            for s in self._available(grants)
        ]

    def visible_names(self, grants: frozenset) -> set[str]:
        return {s.name for s in self._available(grants)}


# ------------------------------------------------------------ argument checks

def validate_args(schema: dict, args: Any) -> Optional[str]:
    """Check arguments against the small JSON-Schema subset this project uses.

    Returns an error message, or None when the arguments are acceptable.
    Unknown keys are rejected: a model that invents an argument is wrong, and
    silently ignoring it hides the mistake.
    """
    if not isinstance(args, dict):
        return "arguments must be an object"
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            return f"missing argument: {key}"
    for key, value in args.items():
        if key not in props:
            return f"unexpected argument: {key}"
        err = _check_value(props[key], value, key)
        if err:
            return err
    return None


def _check_value(spec: dict, value: Any, where: str) -> Optional[str]:
    kind = spec.get("type")
    if value is None:
        return f"{where} must not be null"
    if kind == "string":
        if not isinstance(value, str):
            return f"{where} must be a string"
        if len(value) > min(spec.get("maxLength", _MAX_STRING), _MAX_STRING):
            return f"{where} is too long"
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{where} must be an integer"
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{where} must be a number"
        if value != value or value in (float("inf"), float("-inf")):
            return f"{where} must be finite"
    elif kind == "boolean":
        if not isinstance(value, bool):
            return f"{where} must be true or false"
    elif kind == "array":
        if not isinstance(value, list):
            return f"{where} must be a list"
        if len(value) > spec.get("maxItems", 200):
            return f"{where} has too many items"
        item_spec = spec.get("items")
        if item_spec:
            for i, item in enumerate(value):
                err = _check_value(item_spec, item, f"{where}[{i}]")
                if err:
                    return err
    elif kind == "object":
        if not isinstance(value, dict):
            return f"{where} must be an object"
        if len(json.dumps(value, ensure_ascii=False, default=str)) > 60000:
            return f"{where} is too large"
    if "enum" in spec and value not in spec["enum"]:
        return f"{where} must be one of {', '.join(map(str, spec['enum']))}"
    if kind in ("integer", "number"):
        if "minimum" in spec and value < spec["minimum"]:
            return f"{where} must be at least {spec['minimum']}"
        if "maximum" in spec and value > spec["maximum"]:
            return f"{where} must be at most {spec['maximum']}"
    return None


# -------------------------------------------------------------------- loading

def load_all(modules: tuple[str, ...] = TOOL_MODULES) -> list[ToolSpec]:
    """Import every tool module and collect its SPECS list."""
    specs: list[ToolSpec] = []
    for mod_name in modules:
        try:
            mod = importlib.import_module(mod_name)
        except ModuleNotFoundError as exc:
            if exc.name == mod_name:
                log.info("tool module not present, skipped: %s", mod_name)
                continue
            raise
        specs.extend(getattr(mod, "SPECS", []))
    return specs


def build_registry(modules: tuple[str, ...] = TOOL_MODULES) -> Registry:
    reg = Registry()
    reg.register_many(load_all(modules))
    return reg


def schema_snapshot(reg: Registry, grants: frozenset) -> str:
    """Stable text form of the visible tool surface, for tests and reviews."""
    rows = sorted(
        (s.name, s.family, s.tier.value, s.description, json.dumps(s.parameters, sort_keys=True))
        for s in reg._available(grants)
    )
    return json.dumps(rows, ensure_ascii=False, indent=1)


def verdict_for_unknown(name: str) -> Verdict:
    return Verdict.deny("unknown_tool", f"no tool named {name!r}")
