"""Vocabulary of the governor: pool classes, tunable limits and the errors it raises.

Nothing here performs I/O. The numbers come from section 7 of
docs/architecture-v2.md; the comments say why each rule exists.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class GovClass(str, Enum):
    """The resource class a handler runs under. Values match ``ToolSpec.gov_class``."""

    NONE = "NONE"        # no special resource; shares the TOOL pool
    TOOL = "TOOL"
    NET = "NET"
    LLM = "LLM"
    UIA = "UIA"          # COM is apartment-bound: one thread only
    INPUT = "INPUT"      # one lease; keystrokes must not interleave
    VISION = "VISION"
    BROWSER = "BROWSER"
    OFFICE = "OFFICE"
    SHELL = "SHELL"
    INDEX = "INDEX"
    STT = "STT"


# Classes that yield to the owner. Pressure pauses them, and their threads run at
# background priority on Windows.
BACKGROUND = frozenset({GovClass.INDEX, GovClass.VISION, GovClass.STT})

# In-process classes whose work cannot be stopped once it has started. A timed-out
# job here may still be touching the desktop, the microphone or the screen, so the
# action is recorded as unknown rather than failed.
ABANDONABLE = frozenset({GovClass.UIA, GovClass.INPUT, GovClass.VISION, GovClass.STT})

DEFAULT_POOL_SIZES: dict[GovClass, int] = {
    GovClass.TOOL: 4,
    GovClass.NET: 4,
    GovClass.LLM: 2,
    GovClass.UIA: 1,
    GovClass.INPUT: 1,
    GovClass.VISION: 1,
    GovClass.BROWSER: 1,
    GovClass.OFFICE: 1,
    GovClass.SHELL: 1,
    GovClass.INDEX: 1,
    GovClass.STT: 1,
}


def pool_for(gov_class: GovClass) -> GovClass:
    """The pool a class runs in. ``NONE`` is light work, so it shares the TOOL pool."""
    return GovClass.TOOL if gov_class is GovClass.NONE else gov_class


@dataclass(frozen=True)
class Limits:
    """Every threshold the governor applies. Defaults are the section 7 values.

    Memory is in MiB, the same unit psutil's ``available`` divided by 2**20 gives.
    """

    pool_sizes: Mapping[GovClass, int] = field(default_factory=dict)  # overrides of DEFAULT_POOL_SIZES
    max_waiters: int = 8               # callers queued behind one pool; more fail fast
    abandon_limit: int = 3             # abandoned jobs that disable a pool until restart
    owner_idle_s: float = 5.0          # owner input inside this window pauses background work
    ram_pause_mb: float = 1536.0       # free RAM under 1.5 GB pauses background work
    ram_refuse_mb: float = 800.0       # free RAM under this refuses non-interactive jobs
    battery_pause_pct: int = 25        # unplugged below this pauses background work
    cpu_pause_pct: float = 80.0        # mean CPU above this pauses background work
    cpu_stop_pct: float = 95.0         # mean CPU above this pauses everything non-interactive
    cpu_window_s: float = 10.0         # the CPU mean is taken over this many seconds
    resume_clear_s: float = 30.0       # a condition must stay clear this long before work resumes
    poll_s: float = 0.05               # how often a waiting caller checks the cancel token


class GovError(Exception):
    """Base for the governor's own refusals. ``code`` is the machine-readable reason."""

    code: str = ""


class Refused(GovError):
    """The call was not admitted. Nothing ran, so nothing needs undoing."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class GovTimeout(GovError):
    """The call ran past its limit, or never got a slot in time.

    ``abandoned`` is True only when an in-process job had already started and is
    still running on its thread. The caller must then record the action as unknown.
    """

    code = "timeout"

    def __init__(self, *, abandoned: bool = False) -> None:
        super().__init__("timeout; the job is still running and was abandoned" if abandoned else "timeout")
        self.abandoned = abandoned
