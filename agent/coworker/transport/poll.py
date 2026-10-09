"""Long-poll loop that feeds owner updates to the orchestrator.

Four rules, each for a stated reason:

* The getUpdates offset is written to the store before a batch is handled. A
  crash mid-batch loses the rest of that batch, but a restart never replays
  side effects such as a file sent twice or a job run twice.
* Each update id is compared with the last one handled, so a duplicate is
  skipped instead of run again. Telegram ids only grow, so one number is enough.
* Failures back off from 1 s, doubling to 60 s, so a flaky network does not
  hammer the API.
* HTTP 409 means another consumer holds the bot. Right after a restart that is
  usually the previous process's long poll, still open for up to LONG_POLL_S,
  so a 409 is retried on the same backoff until it has lasted CONFLICT_WINDOW_S.
  Only then is polling stopped, loudly, and the status callback told it is
  offline. A real second consumer still stops polling; it just takes the window
  to say so.

Every update that reaches this loop is recorded as handled, including the ones
the gate dropped. A dropped update is audited once per kind of update per
DROP_WINDOW_S, with a count of the repeats, so a stranger's flood cannot fill
the audit table.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from ..core.types import Decision, Tier
from .bot import LONG_POLL_S

if TYPE_CHECKING:  # pragma: no cover
    from .bot import BotApi
    from .gate import OwnerGate
    from ..store.db import Store

log = logging.getLogger("transport.poll")

OFFSET_KEY = "tg_offset"
LAST_HANDLED_KEY = "tg_last_handled"
BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 60.0
# Longer than one long poll plus its HTTP margin, so the old poll has time to end.
CONFLICT_WINDOW_S = 90.0
DROP_WINDOW_S = 60.0


def describe(update: dict) -> str:
    """Kind and chat type only. Ids and text are left out of the audit row."""
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        return f"kind=callback_query chat={_chat_type(callback.get('message'))}"
    message = update.get("message")
    if isinstance(message, dict):
        return f"kind=message chat={_chat_type(message)}"
    return "kind=other chat=-"


def _chat_type(holder: Any) -> str:
    chat = holder.get("chat") if isinstance(holder, dict) else None
    kind = chat.get("type") if isinstance(chat, dict) else None
    return str(kind or "-")


def _usable(batch: Any) -> list[dict]:
    """Updates with an integer id, in ascending order. A malformed item is skipped."""
    if not isinstance(batch, list):
        return []
    valid = [u for u in batch if isinstance(u, dict) and isinstance(u.get("update_id"), int)]
    return sorted(valid, key=lambda u: u["update_id"])


@dataclass
class _DropWindow:
    """The drops of one kind that began at ``start``, after the first was audited."""

    start: float
    repeats: int = 0


class PollLoop:
    """Runs until ``stop_event`` is set, or until a 409 Conflict has lasted CONFLICT_WINDOW_S.

    ``handle(update)`` receives only updates the gate admitted, after pairing.
    A handler that raises is logged and the update counts as handled, so one
    bad update cannot stall the queue behind it.

    ``on_stopped(reason)`` is called when the loop gives up on a 409, so the
    status shown to the owner stops saying online. ``clock`` is monotonic time;
    tests pass a fake one so the windows can be crossed without waiting.
    """

    def __init__(
        self,
        api: "BotApi",
        store: "Store",
        gate: "OwnerGate",
        handle: Callable[[dict], None],
        *,
        on_stopped: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api = api
        self._store = store
        self._gate = gate
        self._on_update = handle
        self._on_stopped = on_stopped
        self._clock = clock
        self._drop_windows: dict[str, _DropWindow] = {}

    def run(self, stop_event: Any) -> None:
        self._delete_webhook()
        try:
            self._poll(stop_event)
        finally:
            self._flush_repeats()

    def _poll(self, stop_event: Any) -> None:
        offset = int(self._store.kv_get(OFFSET_KEY, 0) or 0)
        delay = BACKOFF_START_S
        conflict_since: float | None = None
        while not stop_event.is_set():
            try:
                res = self._api.get_updates(offset, timeout=LONG_POLL_S)
                if res.get("error_code") == 409:
                    now = self._clock()
                    if conflict_since is None:
                        conflict_since = now
                    if now - conflict_since >= CONFLICT_WINDOW_S:
                        self._give_up_on_conflict()
                        return
                    log.warning("Telegram answered 409 Conflict; retrying in %.0f s", delay)
                    if stop_event.wait(delay):
                        return
                    delay = min(delay * 2, BACKOFF_MAX_S)
                    continue
                if not res.get("ok"):
                    # A network error says nothing about the conflict, so the window keeps running.
                    log.warning(
                        "getUpdates failed (%s); retrying in %.0f s",
                        res.get("error_code") or "network", delay,
                    )
                    if stop_event.wait(delay):
                        return
                    delay = min(delay * 2, BACKOFF_MAX_S)
                    continue

                conflict_since = None
                delay = BACKOFF_START_S
                batch = _usable(res.get("result"))
                if not batch:
                    continue
                offset = max(offset, batch[-1]["update_id"] + 1)
                self._store.kv_set(OFFSET_KEY, offset)
                for update in batch:
                    self._guarded_dispatch(update)
            except Exception:
                log.exception("poll cycle failed; retrying in %.0f s", delay)
                if stop_event.wait(delay):
                    return
                delay = min(delay * 2, BACKOFF_MAX_S)

    def _give_up_on_conflict(self) -> None:
        log.critical(
            "Telegram kept answering 409 Conflict for %.0f s: another consumer holds this bot "
            "(a webhook, or a second Coworker copy). Polling has stopped; remove the conflict "
            "and restart Coworker.",
            CONFLICT_WINDOW_S,
        )
        if self._on_stopped is None:
            return
        try:
            self._on_stopped("Telegram 409 Conflict: polling stopped")
        except Exception:
            # Reporting must not make the loop resume polling after it gave up.
            log.exception("the offline status could not be published")

    def _delete_webhook(self) -> None:
        res = self._api.delete_webhook()
        if not res.get("ok"):
            log.warning("deleteWebhook failed (%s); a 409 will stop polling", res.get("description"))

    def _guarded_dispatch(self, update: dict) -> None:
        """One failure boundary per update, so a bad update cannot take the rest of its batch with it.

        Without this, an update that cannot be audited would stop a later
        /stop or /panic in the same batch from ever being handled.
        """
        try:
            self._dispatch(update)
        except Exception:
            log.exception("update %s could not be processed", update.get("update_id"))

    def _dispatch(self, update: dict) -> None:
        uid = update["update_id"]
        if uid <= int(self._store.kv_get(LAST_HANDLED_KEY, 0) or 0):
            log.info("update %s already handled, skipped", uid)
            return
        if not self._admit(update):
            self._record_drop(update)
        elif not self._gate.paired():
            self._pair(update)
        else:
            self._run_handler(update)
        self._store.kv_set(LAST_HANDLED_KEY, uid)

    def _admit(self, update: dict) -> bool:
        try:
            return self._gate.allows(update)
        except Exception:
            log.exception("gate check failed; update dropped")
            return False

    def _pair(self, update: dict) -> None:
        chat_id, message, accepted = self._gate.redeem(update)
        log.info("pairing attempt: %s", "accepted" if accepted else "refused")
        # Answered straight through the API: until Pairing accepts the code this
        # chat is not the owner's, and the outbox would refuse it.
        self._api.send_message(chat_id, message)

    def _record_drop(self, update: dict) -> None:
        """Audit a dropped update: the first of its kind in a window at once, the rest as a count.

        A stranger can message the bot as fast as Telegram delivers, so one row
        per drop would let that flood fill the audit table and push the owner's
        own rows out of /audit. The count of the repeats is written when the
        window ends, or at the next stop.
        """
        key = describe(update)
        now = self._clock()
        window = self._drop_windows.get(key)
        if window is not None and now - window.start < DROP_WINDOW_S:
            window.repeats += 1
            return
        if window is not None and window.repeats:
            self._audit_drop(f"{key} repeats={window.repeats}")
        self._drop_windows[key] = _DropWindow(start=now)
        self._audit_drop(key)

    def _flush_repeats(self) -> None:
        """Write the repeat counts still held for open windows, so stopping does not lose them."""
        for key, window in self._drop_windows.items():
            if not window.repeats:
                continue
            try:
                self._audit_drop(f"{key} repeats={window.repeats}")
            except Exception:
                log.exception("drop counts for %s could not be written", key)
                continue
            window.repeats = 0

    def _audit_drop(self, summary: str) -> None:
        intent = self._store.audit_intent(
            turn_id="telegram",
            actor="gate",
            tool="telegram_update",
            tier=Tier.READ.value,
            decision=Decision.DENY.value,
            code="gate_dropped",
            args_summary=summary,
            provider="telegram",
        )
        self._store.audit_outcome(intent, ok=False, code="gate_dropped", summary="dropped before parsing")

    def _run_handler(self, update: dict) -> None:
        try:
            self._on_update(update)
        except Exception:
            log.exception("update %s failed in the handler", update.get("update_id"))
