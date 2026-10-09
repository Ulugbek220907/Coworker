"""Shared vocabulary for the policy kernel, the tool layer and the orchestrator.

Nothing in this module performs I/O. Every decision about whether a side effect
may happen is expressed with these types, so the decision logic can be tested
without a desktop, a network or a model.
"""
from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Iterable, Optional


class Tier(str, Enum):
    """Risk tier of an action. Every tool declares exactly one."""

    READ = "READ"
    LOCAL_WRITE = "LOCAL_WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"
    OUTBOUND = "OUTBOUND"
    SYSTEM_CHANGE = "SYSTEM_CHANGE"
    FINANCIAL = "FINANCIAL"    # never allowed: the registry refuses such tools
    CREDENTIAL = "CREDENTIAL"  # never allowed: the registry refuses such tools


class Decision(str, Enum):
    ALLOW = "ALLOW"
    CONFIRM = "CONFIRM"
    DENY = "DENY"


class Autonomy(str, Enum):
    """Global autonomy level. One level applies to every tool family."""

    ASK_ALWAYS = "ask_always"                    # AA: every non-read action is confirmed
    ASK_FOR_WRITES = "ask_for_writes"            # AW: default
    AUTONOMOUS_READONLY = "autonomous_readonly"  # AR: reads and internal notes only; unattended ceiling
    PANIC = "panic"                              # P: everything denied except /status and /help


class Provenance(IntEnum):
    """Where the words that drove a turn came from. Ordered: the highest wins."""

    OWNER = 1     # typed or spoken by the owner
    METADATA = 2  # file names, paths and window titles returned by searches
    CONTENT = 3   # text read from documents, pages, screens and mail bodies


_RANK = {Decision.ALLOW: 0, Decision.CONFIRM: 1, Decision.DENY: 2}


@dataclass(frozen=True)
class Verdict:
    """The outcome of one policy evaluation. ``code`` is machine-readable."""

    decision: Decision
    code: str
    reason: str = ""
    summary: str = ""          # owner-facing text shown on a CONFIRM card
    two_channel: bool = False  # also needs a local approval from the desktop UI

    @classmethod
    def allow(cls, code: str = "allowed", reason: str = "") -> "Verdict":
        return cls(Decision.ALLOW, code, reason)

    @classmethod
    def confirm(cls, code: str, reason: str = "", summary: str = "", two_channel: bool = False) -> "Verdict":
        return cls(Decision.CONFIRM, code, reason, summary, two_channel)

    @classmethod
    def deny(cls, code: str, reason: str = "") -> "Verdict":
        return cls(Decision.DENY, code, reason)


def strictest(verdicts: Iterable[Verdict]) -> Verdict:
    """Combine verdicts: DENY beats CONFIRM beats ALLOW.

    The winning verdict keeps the first summary and the first code at its level,
    and two_channel is set if any verdict at that level requires it. With no
    verdicts at all the answer is ALLOW - callers add their own default first.
    """
    items = list(verdicts)
    if not items:
        return Verdict.allow("no_rule")
    top = max(_RANK[v.decision] for v in items)
    level = [v for v in items if _RANK[v.decision] == top]
    head = level[0]
    summary = next((v.summary for v in level if v.summary), "")
    two = any(v.two_channel for v in level)
    return Verdict(head.decision, head.code, head.reason, summary, two)


class Cancelled(Exception):
    """Raised inside a step when the owner's kill switch has fired."""


class CancelToken:
    """Cooperative cancellation. /stop sets it; long steps call ``check()``."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled("cancelled")


_SPACES = re.compile(r"\s+")
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "ʻ": "'", "`": "'", "´": "'"})


def normalize_text(text: str) -> str:
    """Comparable form of a piece of text: NFKC, casefolded, one apostrophe, single spaces."""
    text = unicodedata.normalize("NFKC", text or "").translate(_APOSTROPHES).casefold()
    return _SPACES.sub(" ", text).strip()


@dataclass(frozen=True)
class CallContext:
    """Immutable snapshot taken for every single tool call.

    The orchestrator builds one per call from its per-turn state. No tool or
    policy rule may keep state across calls through this object.
    """

    turn_id: str
    actor: str                       # "owner" or "scheduler"
    chat_id: Optional[int]           # the owner's private chat; None for scheduled runs
    autonomy: Autonomy
    grants: frozenset                # enabled tool families
    generation: int                  # kill-switch generation this call belongs to
    provenance: Provenance           # highest provenance seen in this turn so far
    owner_norm: str = ""             # normalised owner text of this turn
    content_norm: str = ""           # normalised untrusted text seen so far in this turn
    surfaced: frozenset = frozenset()  # normalised paths returned by this turn's searches
    # The subset of those whose search query came from the owner's own words. Only these are
    # exempt from the origin check: a search steered by a document is not the owner's choice.
    surfaced_owner: frozenset = frozenset()
    delivered: frozenset = frozenset() # normalised paths already delivered to the owner
    local_read: bool = False         # this turn already read local data (files, screen, clipboard)
    cancel: Optional[CancelToken] = None


@dataclass
class ToolResult:
    """What a tool returns. ``untrusted`` marks output that came from content."""

    ok: bool
    data: dict = field(default_factory=dict)
    error: str = ""
    code: str = ""
    untrusted: bool = False
    surfaced: tuple = ()   # paths returned by a search; become sendable this turn
    sendable: tuple = ()   # files created by the tool (for example a PDF) the owner may receive

    @classmethod
    def fail(cls, code: str, error: str, **data: Any) -> "ToolResult":
        return cls(ok=False, data=dict(data), error=error, code=code)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"ok": self.ok}
        if self.error:
            out["error"] = self.error
        if self.code:
            out["code"] = self.code
        out.update(self.data)
        return out
