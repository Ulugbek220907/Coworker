"""Pressure signals with hysteresis: when background work may run.

Each condition is a gate. A gate trips the moment its condition holds, and it
clears only after the condition has stayed false for ``resume_clear_s`` seconds.
A reading that hovers around a threshold therefore cannot switch background work
on and off every few seconds. The CPU rule uses the mean over a window of samples,
so one spike pauses nothing.

Samples are taken when the governor starts a call or reports its status. There is
no sampler thread: the windows and clear timers are measured between those samples,
which is the only time the answer is needed.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable

from ..core.ports import OsPort, PowerState
from .model import Limits

log = logging.getLogger("governor")

# Gate names, in the order they are reported.
OWNER_ACTIVE = "owner_active"
BATTERY_SAVER = "battery_saver"
LOW_RAM = "low_ram"
CPU_HIGH = "cpu_high"
BATTERY_LOW = "battery_low"
CPU_CRITICAL = "cpu_critical"
GATES = (OWNER_ACTIVE, BATTERY_SAVER, LOW_RAM, CPU_HIGH, BATTERY_LOW, CPU_CRITICAL)
_BACKGROUND_GATES = frozenset({OWNER_ACTIVE, BATTERY_SAVER, LOW_RAM, CPU_HIGH, BATTERY_LOW})


@dataclass(frozen=True)
class Snapshot:
    idle_s: float
    cpu_pct: float          # the reading taken in this sample
    cpu_mean_pct: float     # mean over the CPU window; the gates use this
    free_ram_mb: float
    power: PowerState
    input_desktop: bool


@dataclass(frozen=True)
class PressureState:
    snapshot: Snapshot
    reasons: tuple[str, ...]    # the gates that are tripped right now

    @property
    def background_paused(self) -> bool:
        return any(reason in _BACKGROUND_GATES for reason in self.reasons)

    @property
    def all_paused(self) -> bool:
        return CPU_CRITICAL in self.reasons


class _Gate:
    """One condition with time hysteresis: trips at once, clears after a quiet period."""

    def __init__(self) -> None:
        self.tripped = False
        self._clear_since: float | None = None

    def update(self, raw: bool, now: float, resume_clear_s: float) -> bool:
        if raw:
            self.tripped = True
            self._clear_since = None
        elif self.tripped:
            if self._clear_since is None:
                self._clear_since = now
            elif now - self._clear_since >= resume_clear_s:
                self.tripped = False
                self._clear_since = None
        return self.tripped


class Pressure:
    """Reads the OS, updates the gates, and reports which conditions are tripped."""

    def __init__(self, os_port: OsPort, limits: Limits, clock: Callable[[], float]) -> None:
        self._os = os_port
        self._limits = limits
        self._clock = clock
        self._lock = threading.Lock()
        self._cpu_window: deque[tuple[float, float]] = deque()
        self._gates = {name: _Gate() for name in GATES}
        self._reasons: tuple[str, ...] = ()

    def sample(self) -> PressureState:
        """Read every signal, then update the gates. Blocks for the CPU sample; keep it off the event loop."""
        idle = self._os.idle_seconds()
        cpu = self._os.cpu_percent()
        ram = self._os.free_ram_mb()
        power = self._os.power()
        desktop = self._os.input_desktop_available()
        now = self._clock()
        limits = self._limits
        with self._lock:
            window = self._cpu_window
            window.append((now, cpu))
            while now - window[0][0] > limits.cpu_window_s:
                window.popleft()
            mean = sum(value for _, value in window) / len(window)
            snapshot = Snapshot(
                idle_s=idle, cpu_pct=cpu, cpu_mean_pct=mean, free_ram_mb=ram,
                power=power, input_desktop=desktop,
            )
            raw = {
                OWNER_ACTIVE: idle < limits.owner_idle_s,
                BATTERY_SAVER: power.saver,
                LOW_RAM: ram < limits.ram_pause_mb,
                CPU_HIGH: mean > limits.cpu_pause_pct,
                BATTERY_LOW: (
                    power.plugged is False and power.percent is not None
                    and power.percent < limits.battery_pause_pct
                ),
                CPU_CRITICAL: mean > limits.cpu_stop_pct,
            }
            reasons = tuple(
                name for name in GATES
                if self._gates[name].update(raw[name], now, limits.resume_clear_s)
            )
            if reasons != self._reasons:
                log.info("pressure: %s", ", ".join(reasons) or "clear")
                self._reasons = reasons
        return PressureState(snapshot=snapshot, reasons=reasons)
