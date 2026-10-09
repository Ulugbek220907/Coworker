"""The governor: the one door through which heavy handlers run.

``run()`` checks pressure, admits the call to its pool, runs the function in a pool
thread and waits for the result while polling the cancel token. The timeout covers
the whole call, including any wait for a slot, so a caller always gets an answer
within ``timeout_s``.

Python cannot stop a running thread. A job that times out keeps its thread until it
returns, and it keeps its slot until then. For the in-process classes
(``ABANDONABLE``) the caller is told the action is unknown, and three such jobs
disable that pool until restart.
"""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from ..core.ports import OsPort
from ..core.types import CancelToken, Cancelled
from .model import ABANDONABLE, BACKGROUND, DEFAULT_POOL_SIZES, GovClass, GovTimeout, Limits, Refused, pool_for
from .pool import Pool
from .pressure import Pressure, PressureState
from .signals import init_com_apartment, lower_thread_priority

log = logging.getLogger("governor")

# Thread setup run once in each pool thread: COM for UIA, background priority for the rest.
_THREAD_SETUP: dict[GovClass, Callable[[], object]] = {
    GovClass.UIA: init_com_apartment,
    **{gov_class: lower_thread_priority for gov_class in BACKGROUND},
}


class Governor:
    """Runs handler functions under pools, pressure rules and timeouts.

    ``monotonic`` drives the pressure windows and the clear timers only. Job
    deadlines use the real clock, so a frozen fake clock can never hang a wait.
    """

    def __init__(
        self,
        os_port: OsPort,
        *,
        limits: Limits | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = limits or Limits()
        self._pressure = Pressure(os_port, self._limits, monotonic)
        sizes = {**DEFAULT_POOL_SIZES, **self._limits.pool_sizes}
        self._pools: dict[GovClass, Pool] = {
            gov_class: Pool(
                gov_class,
                size,
                max_waiters=self._limits.max_waiters,
                abandon_limit=self._limits.abandon_limit,
                initializer=_THREAD_SETUP.get(gov_class),
            )
            for gov_class, size in sizes.items()
        }
        # Sampling can block for the CPU reading, so it runs on its own thread, not the loop.
        self._sampler = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gov-sample")

    async def run(
        self,
        gov_class: GovClass | str,
        fn: Callable[[], Any],
        *,
        timeout_s: float,
        cancel: CancelToken | None = None,
        interactive: bool = True,
    ) -> Any:
        """Run ``fn`` in a pool thread and return its result.

        ``interactive`` marks a turn the owner is waiting for. Interactive calls
        are exempt from the pressure pauses and from the low-memory refusal; the
        locked-desktop refusal, the waiter cap and disabled pools still apply.

        Raises ``Refused`` (throttled, low_memory, locked_desktop, paused),
        ``GovTimeout`` (timeout) or ``Cancelled``. The function's own exceptions
        propagate unchanged.
        """
        cls = GovClass(gov_class)
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if cancel is not None:
            cancel.check()
        deadline = time.monotonic() + timeout_s
        state = await asyncio.get_running_loop().run_in_executor(self._sampler, self._pressure.sample)
        refusal = self._refusal(cls, interactive, state)
        if refusal is not None:
            raise refusal
        pool = self._pools[pool_for(cls)]
        await self._acquire(pool, deadline, cancel)
        future = pool.submit(fn)
        return await self._wait(pool, cls, future, deadline, cancel)

    def pressure(self) -> dict:
        """The signals and the paused state, for the tray and /status. Takes a fresh sample.

        The sample can block for the CPU reading, so call this from a thread, not the event loop.
        """
        state = self._pressure.sample()
        snap = state.snapshot
        return {
            "background_paused": state.background_paused,
            "all_paused": state.all_paused,
            "reasons": list(state.reasons),
            "idle_s": round(snap.idle_s, 1),
            "cpu_pct": round(snap.cpu_pct, 1),
            "cpu_mean_pct": round(snap.cpu_mean_pct, 1),
            "free_ram_mb": round(snap.free_ram_mb),
            "battery_pct": snap.power.percent,
            "plugged": snap.power.plugged,
            "battery_saver": snap.power.saver,
            "input_desktop": snap.input_desktop,
        }

    def status(self) -> dict:
        """Per-pool counters: slots, waiters, abandoned jobs and whether the pool is disabled."""
        return {"pools": {gov_class.value: pool.snapshot() for gov_class, pool in self._pools.items()}}

    # ------------------------------------------------------------------ helpers

    def _refusal(self, gov_class: GovClass, interactive: bool, state: PressureState) -> Refused | None:
        snap = state.snapshot
        if gov_class is GovClass.INPUT and not snap.input_desktop:
            return Refused("locked_desktop", "the desktop is locked; input cannot be sent")
        if interactive:
            return None
        if snap.free_ram_mb < self._limits.ram_refuse_mb:
            return Refused("low_memory", "free memory is too low for unattended work")
        if state.all_paused or (gov_class in BACKGROUND and state.background_paused):
            return Refused("paused", "paused under pressure: " + ", ".join(state.reasons))
        return None

    async def _acquire(self, pool: Pool, deadline: float, cancel: CancelToken | None) -> None:
        if pool.try_take():
            return
        pool.enter_queue()
        try:
            while True:
                if cancel is not None:
                    cancel.check()
                if time.monotonic() >= deadline:
                    raise GovTimeout()
                await asyncio.sleep(self._limits.poll_s)
                if pool.try_take():
                    return
        finally:
            pool.leave_queue()

    async def _wait(
        self,
        pool: Pool,
        gov_class: GovClass,
        future: Future,
        deadline: float,
        cancel: CancelToken | None,
    ) -> Any:
        waiter = asyncio.wrap_future(future)
        try:
            while True:
                if cancel is not None and cancel.cancelled:
                    future.cancel()
                    raise Cancelled("cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                done, _ = await asyncio.wait({waiter}, timeout=min(self._limits.poll_s, remaining))
                if done:
                    return waiter.result()
            if future.done():
                return await waiter
            raise _timed_out(pool, gov_class, future)
        except BaseException:
            waiter.add_done_callback(_discard)
            raise


def _timed_out(pool: Pool, gov_class: GovClass, future: Future) -> GovTimeout:
    if future.cancel():
        return GovTimeout(abandoned=False)  # it never started, so nothing ran
    if gov_class not in ABANDONABLE:
        log.warning("%s job exceeded its time limit; it keeps its slot until it returns", gov_class.value)
        return GovTimeout(abandoned=False)
    if pool.record_abandoned():
        log.error("%s disabled until restart after repeated abandoned jobs", gov_class.value)
    log.warning("%s job abandoned after its time limit; its result is dropped", gov_class.value)
    return GovTimeout(abandoned=True)


def _discard(waiter: asyncio.Future) -> None:
    # A result nobody will read still counts as retrieved, so asyncio does not log it.
    if not waiter.cancelled():
        waiter.exception()
