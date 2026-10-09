"""The operating-system signals the governor needs, behind a small protocol.

The real implementation lives in governor/signals.py and reads psutil and
Win32 calls. Tests pass a fake that returns scripted values, so the pause and
resume rules can be checked without a loaded machine.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class PowerState:
    percent: Optional[int]     # None when the machine has no battery
    plugged: Optional[bool]    # None when unknown
    saver: bool                # Windows battery saver on


class OsPort(Protocol):
    def idle_seconds(self) -> float:
        """Seconds since the owner last touched keyboard or mouse."""

    def cpu_percent(self) -> float:
        """Recent whole-machine CPU load, 0-100."""

    def free_ram_mb(self) -> float:
        """Available physical memory in megabytes."""

    def power(self) -> PowerState:
        """Battery and power-saving state."""

    def is_elevated(self) -> Optional[bool]:
        """True or False for this process's elevation; None when the probe failed."""

    def input_desktop_available(self) -> bool:
        """False on the lock screen or a secure desktop, where input cannot be sent."""
