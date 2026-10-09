"""The kill switch: stop everything now, and panic until someone resumes locally.

Two mechanisms. The first is the generation counter, kept in the store. Every
call carries the generation it was born in, and work that is still queued when
the counter moves on is refused where it would run. That is how queued work is
dropped without the switch having to reach into the governor's queues. The
second is cancellation: running steps hold a CancelToken and stop at their next
check.

Panic is the same stop, plus a persisted flag. The flag survives a restart, so
a machine that was in panic when it went down is still in panic when it comes
back. Only a local call clears it: ``resume_local`` takes no arguments, and no
Telegram command is wired to it.
"""
from __future__ import annotations

import threading

from ..core.types import CancelToken
from ..store import Store
from ..store.base import KILL_GENERATION_KEY

PANIC_KEY = "panic"


class KillSwitch:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._tokens: set[CancelToken] = set()
        self._lock = threading.Lock()

    @property
    def generation(self) -> int:
        return int(self._store.kv_get(KILL_GENERATION_KEY, 0))

    def is_current(self, generation: int) -> bool:
        """False once a stop or panic has happened after ``generation`` was issued."""
        return generation == self.generation

    def register(self, token: CancelToken) -> None:
        with self._lock:
            self._tokens.add(token)

    def unregister(self, token: CancelToken) -> None:
        with self._lock:
            self._tokens.discard(token)

    def stop(self) -> None:
        """Cancel running work, cancel pending approvals and drop queued work."""
        self._store.kv_increment(KILL_GENERATION_KEY)
        with self._lock:
            tokens, self._tokens = list(self._tokens), set()
        for token in tokens:
            token.cancel()
        self._store.approval_cancel_pending("stop")

    def panic(self) -> None:
        """Stop, then refuse everything until ``resume_local`` is called on this machine."""
        self._store.kv_set(PANIC_KEY, True)
        self.stop()

    def is_panic(self) -> bool:
        return bool(self._store.kv_get(PANIC_KEY, False))

    def resume_local(self) -> bool:
        """Clear panic. Returns False when the switch was not in panic."""
        if not self.is_panic():
            return False
        self._store.kv_set(PANIC_KEY, False)
        return True
