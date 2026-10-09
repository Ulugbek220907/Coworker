"""One governor pool: a bounded thread pool plus the slot and waiter bookkeeping.

Slots are counted here, not in the executor. A job takes a slot before it is
submitted, and the slot is returned when the job's function actually returns, not
when the caller stops waiting. A job that timed out but is still running therefore
keeps its slot. The executor never has more work than it has threads, so nothing
queues invisibly inside it.

The counters use a threading lock because worker threads release slots. Waiting
for a slot is async and is done by the governor.
"""
from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from .model import GovClass, Refused


class Pool:
    def __init__(
        self,
        gov_class: GovClass,
        size: int,
        *,
        max_waiters: int,
        abandon_limit: int,
        initializer: Callable[[], object] | None = None,
    ) -> None:
        self.gov_class = gov_class
        self.size = size
        self._max_waiters = max_waiters
        self._abandon_limit = abandon_limit
        self._executor = ThreadPoolExecutor(
            max_workers=size,
            thread_name_prefix=f"gov-{gov_class.value.lower()}",
            initializer=initializer,
        )
        self._lock = threading.Lock()
        self._running = 0
        self._waiting = 0
        self._abandoned = 0
        self._disabled = False

    def try_take(self) -> bool:
        """Take a slot if one is free. Raises Refused once the pool is disabled."""
        with self._lock:
            self._check_enabled()
            if self._running < self.size:
                self._running += 1
                return True
            return False

    def enter_queue(self) -> None:
        """Register one waiting caller. Fails fast when the waiter cap is reached."""
        with self._lock:
            self._check_enabled()
            if self._waiting >= self._max_waiters:
                raise Refused("throttled", f"{self.gov_class.value}: too many callers are already waiting")
            self._waiting += 1

    def leave_queue(self) -> None:
        with self._lock:
            self._waiting -= 1

    def submit(self, fn: Callable[[], Any]) -> Future:
        """Start a job in a pool thread. The caller must already hold a slot."""
        future = self._executor.submit(fn)
        future.add_done_callback(self._release)
        return future

    def record_abandoned(self) -> bool:
        """Count one abandoned job. Returns True when this one disabled the pool."""
        with self._lock:
            self._abandoned += 1
            if self._abandoned >= self._abandon_limit and not self._disabled:
                self._disabled = True
                return True
            return False

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "limit": self.size,
                "running": self._running,
                "waiting": self._waiting,
                "abandoned": self._abandoned,
                "disabled": self._disabled,
            }

    def _release(self, _future: Future) -> None:
        with self._lock:
            self._running -= 1

    def _check_enabled(self) -> None:
        if self._disabled:
            raise Refused(
                "throttled",
                f"{self.gov_class.value} is disabled until restart after {self._abandon_limit} abandoned jobs",
            )
