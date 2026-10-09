"""Daily and per-minute budgets for what the agent does.

The day ceiling is weighted: a shell command or a screen capture costs more than a
file read, so a run of heavy actions uses up the day sooner than a run of light
ones. Counted caps bound actions whose number matters on its own, whatever they
weigh. Per-minute rate buckets stop a runaway loop within a minute. They live in
memory on purpose: a restart clears them, while the day counters are in the store
and survive a restart.

Calls are classified from what the dispatcher already has: tool name, family and
tier. Two conventions the dispatcher must keep:
  * an LLM round is admitted with family "llm";
  * VISION_TOOLS is the catalogue's VISION-class set from docs/architecture-v2.md.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

from ..core.types import Tier, Verdict

DAILY_UNITS = 600
WARN_PERCENT = 80
BYTES_PER_DAY = 500 * 1024 * 1024
MINUTE_S = 60.0
OWNER_CHAT_KEY = "owner_chat_id"    # pinned by pairing in the store's kv

TIER_WEIGHT: dict[Tier, int] = {
    Tier.READ: 1,
    Tier.LOCAL_WRITE: 3,
    Tier.DESTRUCTIVE: 10,
    Tier.OUTBOUND: 10,
    Tier.SYSTEM_CHANGE: 10,
}
SHELL_WEIGHT = 10
VISION_WEIGHT = 5
LLM_WEIGHT = 1
UNLISTED_WEIGHT = 10      # a tier missing from the table costs the most

COUNTED_CAPS: dict[str, int] = {
    "destructive": 30,
    "outbound": 20,            # sends to the owner's own chat do not count
    "system_change": 30,
    "shell_free_text": 10,     # shell commands that are not in the fixed table
    "llm_rounds": 1500,
}

RATE_PER_MINUTE: dict[str, int] = {
    "all": 60,                 # every tool call; LLM rounds have their own bucket
    "read": 30,
    "local_write": 10,
    "ui_input": 20,            # desktop_control family
    "browser": 20,
    "outbound": 6,             # outbound that is not Telegram, such as typing into an app
    "system_change": 6,
    "shell": 6,
    "vision": 6,
    "llm": 30,
}
TELEGRAM_PER_CHAT = 20         # Telegram sends have their own bucket per chat

VISION_TOOLS = frozenset({"screen_read", "control_app_read", "web_screenshot"})


class _KvStore(Protocol):
    def kv_get(self, key: str, default: object = None) -> object: ...
    def kv_set(self, key: str, value: object) -> None: ...


@dataclass(frozen=True)
class _Plan:
    weight: int
    counted: tuple[str, ...]          # counted caps this call draws on
    rates: tuple[tuple[str, int], ...]  # (bucket, calls per minute) this call draws on


def _plan(tool: str, family: str, tier: Tier, chat_id: int | None, owner_chat: int | None) -> _Plan:
    if family == "llm":
        return _Plan(LLM_WEIGHT, ("llm_rounds",), (("llm", RATE_PER_MINUTE["llm"]),))

    if tool in VISION_TOOLS:
        weight = VISION_WEIGHT
    elif family == "shell":
        weight = SHELL_WEIGHT
    else:
        weight = TIER_WEIGHT.get(tier, UNLISTED_WEIGHT)

    owner_send = chat_id is not None and chat_id == owner_chat
    counted: list[str] = []
    if tier is Tier.DESTRUCTIVE:
        counted.append("destructive")
    if tier is Tier.SYSTEM_CHANGE:
        counted.append("system_change")
    if tier is Tier.OUTBOUND and not owner_send:
        counted.append("outbound")
    if family == "shell" and tier is not Tier.READ:
        counted.append("shell_free_text")

    rates: list[tuple[str, int]] = [("all", RATE_PER_MINUTE["all"])]
    if tier is Tier.READ:
        rates.append(("read", RATE_PER_MINUTE["read"]))
    elif tier is Tier.LOCAL_WRITE:
        rates.append(("local_write", RATE_PER_MINUTE["local_write"]))
    elif tier is Tier.SYSTEM_CHANGE:
        rates.append(("system_change", RATE_PER_MINUTE["system_change"]))
    if family == "telegram":
        rates.append((f"telegram:{chat_id}", TELEGRAM_PER_CHAT))
    elif tier is Tier.OUTBOUND:
        rates.append(("outbound", RATE_PER_MINUTE["outbound"]))
    if family == "shell":
        rates.append(("shell", RATE_PER_MINUTE["shell"]))
    if family == "browser":
        rates.append(("browser", RATE_PER_MINUTE["browser"]))
    if family == "desktop_control":
        rates.append(("ui_input", RATE_PER_MINUTE["ui_input"]))
    if tool in VISION_TOOLS:
        rates.append(("vision", RATE_PER_MINUTE["vision"]))
    return _Plan(weight, tuple(counted), tuple(rates))


class _RateBuckets:
    """Times of recent calls per bucket. Only the last minute is kept."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}

    def has_room(self, key: str, limit: int, now: float) -> bool:
        hits = self._hits.get(key)
        if hits is None:
            return True
        while hits and now - hits[0] >= MINUTE_S:
            hits.popleft()
        return len(hits) < limit

    def record(self, key: str, now: float) -> None:
        self._hits.setdefault(key, deque()).append(now)


def _day(now: float) -> str:
    return datetime.fromtimestamp(now).date().isoformat()  # the local day, as the owner sees it


def _key(day: str, name: str) -> str:
    return f"budget:{day}:{name}"


class Budget:
    """Admission for tool calls and outgoing bytes. Thread-safe.

    Day counters go through ``store.kv_get`` and ``store.kv_set``. A denied call
    charges nothing: every check runs before the first counter is written.
    """

    def __init__(self, store: _KvStore, *, clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._clock = clock
        self._rates = _RateBuckets()
        self._lock = threading.Lock()

    def admit(self, tool: str, family: str, tier: Tier | str, actor: str, chat_id: int | None) -> Verdict:
        tier = Tier(tier)   # plan() compares by identity, so a plain "READ" must become the member
        with self._lock:
            now = self._clock()
            day = _day(now)
            plan = _plan(tool, family, tier, chat_id, self._owner_chat())
            units = self._count(day, "units")
            if units + plan.weight > DAILY_UNITS:
                return Verdict.deny(
                    "budget_exceeded",
                    f"{actor}: the daily budget is used up ({units} of {DAILY_UNITS} units)",
                )
            for name in plan.counted:
                cap = COUNTED_CAPS[name]
                if self._count(day, name) >= cap:
                    return Verdict.deny("budget_exceeded", f"{actor}: the daily {name} limit of {cap} is reached")
            for key, limit in plan.rates:
                if not self._rates.has_room(key, limit, now):
                    return Verdict.deny("rate_limited", f"{actor}: too many {key} calls in the last minute")
            self._put(day, "units", units + plan.weight)
            for name in plan.counted:
                self._put(day, name, self._count(day, name) + 1)
            for key, _ in plan.rates:
                self._rates.record(key, now)
            return Verdict.allow()

    def admit_bytes(self, n: int, chat_id: int | None) -> Verdict:
        """Charge ``n`` outgoing bytes against this chat's daily allowance."""
        if n < 0:
            return Verdict.deny("arg_invalid", "byte count must not be negative")
        with self._lock:
            day = _day(self._clock())
            name = f"bytes:{chat_id}"
            used = self._count(day, name)
            if used + n > BYTES_PER_DAY:
                return Verdict.deny("budget_exceeded", "the daily byte limit is reached for this chat")
            self._put(day, name, used + n)
            return Verdict.allow()

    def warning_due(self) -> bool:
        """True once per local day, when usage first reaches WARN_PERCENT of the ceiling.

        The caller that gets True is expected to tell the owner. The flag is set
        on the True answer, so a later call the same day returns False.
        """
        with self._lock:
            day = _day(self._clock())
            if self._count(day, "units") * 100 < DAILY_UNITS * WARN_PERCENT:
                return False
            flag = _key(day, "warned")
            if self._store.kv_get(flag):
                return False
            self._store.kv_set(flag, True)
            return True

    def usage(self) -> dict:
        with self._lock:
            day = _day(self._clock())
            return {
                "day": day,
                "units": {"used": self._count(day, "units"), "ceiling": DAILY_UNITS},
                "counts": {name: {"used": self._count(day, name), "cap": cap} for name, cap in COUNTED_CAPS.items()},
                "warned": bool(self._store.kv_get(_key(day, "warned"))),
            }

    # ------------------------------------------------------------------ helpers

    def _owner_chat(self) -> int | None:
        value = self._store.kv_get(OWNER_CHAT_KEY)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _count(self, day: str, name: str) -> int:
        return int(self._store.kv_get(_key(day, name), 0) or 0)

    def _put(self, day: str, name: str, value: int) -> None:
        self._store.kv_set(_key(day, name), value)
