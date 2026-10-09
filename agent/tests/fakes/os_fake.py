"""A scripted OsPort. Tests set the values; the governor reads them.

Every value is a plain attribute, so a test can change the machine between two
governor calls to walk it through a pause and a resume.
"""
from __future__ import annotations

from coworker.core.ports import PowerState


class FakeOs:
    def __init__(
        self,
        *,
        idle_s: float = 600.0,
        cpu_pct: float = 5.0,
        ram_mb: float = 8192.0,
        battery_pct: int | None = None,
        plugged: bool | None = None,
        saver: bool = False,
        elevated: bool = False,
        input_desktop: bool = True,
    ) -> None:
        self.idle_s = idle_s
        self.cpu_pct = cpu_pct
        self.ram_mb = ram_mb
        self.battery_pct = battery_pct
        self.plugged = plugged
        self.saver = saver
        self.elevated = elevated
        self.input_desktop = input_desktop

    def idle_seconds(self) -> float:
        return self.idle_s

    def cpu_percent(self) -> float:
        return self.cpu_pct

    def free_ram_mb(self) -> float:
        return self.ram_mb

    def power(self) -> PowerState:
        return PowerState(percent=self.battery_pct, plugged=self.plugged, saver=self.saver)

    def is_elevated(self) -> bool:
        return self.elevated

    def input_desktop_available(self) -> bool:
        return self.input_desktop
