"""Stand-ins for the Telegram transport's collaborators. No network, no real chat.

FakeBotApi records every call and answers from scripts. It keeps Telegram's
offset rule: get_updates(offset) returns the queued updates with an id of at
least ``offset``, so an update the loop has confirmed does not come back on a
restart. FakeStore keeps the kv, audit and delivery calls in memory and logs
the kv writes in order, so a test can check when the offset was written
relative to a handler call. FakePairing returns a fixed owner.
"""
from __future__ import annotations

from typing import Any, Callable

from coworker.core.types import Verdict


class FakeBotApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.queued_updates: list[dict] = []
        self.answers: dict[str, list[dict]] = {}   # method -> FIFO of scripted answers
        self.poll_script: list[dict] = []          # get_updates answers served before the queue
        # When set, an empty get_updates sets this event, so a polling loop ends.
        self.stop_when_idle: "FakeStopEvent | None" = None

    def script(self, method: str, *answers: dict) -> None:
        self.answers.setdefault(method, []).extend(answers)

    def _answer(self, method: str, **kwargs: Any) -> dict:
        self.calls.append((method, kwargs))
        queue = self.answers.get(method)
        if queue:
            return queue.pop(0)
        return {"ok": True, "result": {}}

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]

    def send_message(self, chat_id: int, text: str, buttons: Any = None, reply_to: Any = None) -> dict:
        return self._answer("send_message", chat_id=chat_id, text=text, buttons=buttons, reply_to=reply_to)

    def send_document(self, chat_id: int, path: str, caption: str = "") -> dict:
        return self._answer("send_document", chat_id=chat_id, path=path, caption=caption)

    def send_photo(self, chat_id: int, path: str, caption: str = "") -> dict:
        return self._answer("send_photo", chat_id=chat_id, path=path, caption=caption)

    def delete_webhook(self) -> dict:
        return self._answer("delete_webhook")

    def get_updates(self, offset: int, timeout: int = 25) -> dict:
        self.calls.append(("get_updates", {"offset": offset, "timeout": timeout}))
        if self.poll_script:
            return self.poll_script.pop(0)
        batch = [u for u in self.queued_updates if u["update_id"] >= offset]
        if not batch and self.stop_when_idle is not None:
            self.stop_when_idle.set()
        return {"ok": True, "result": batch}


class FakeStore:
    def __init__(self) -> None:
        self.kv: dict[str, Any] = {}
        self.events: list[tuple] = []   # ("kv_set", key, value) in call order
        self.audit_rows: list[dict] = []
        self.outcomes: list[dict] = []
        self.delivered: list[tuple] = []

    def kv_get(self, key: str, default: Any = None) -> Any:
        return self.kv.get(key, default)

    def kv_set(self, key: str, value: Any) -> None:
        self.kv[key] = value
        self.events.append(("kv_set", key, value))

    def audit_intent(
        self, *, turn_id: str, actor: str, tool: str, tier: Any, decision: Any,
        code: str, args_summary: str, provider: str,
    ) -> int:
        self.audit_rows.append({
            "turn_id": turn_id, "actor": actor, "tool": tool, "tier": tier,
            "decision": decision, "code": code, "args_summary": args_summary, "provider": provider,
        })
        return len(self.audit_rows)

    def audit_outcome(self, intent_id: int, ok: bool, code: str, summary: str) -> None:
        self.outcomes.append({"intent_id": intent_id, "ok": ok, "code": code, "summary": summary})

    def delivered_add(self, chat_id: int, path: str, name: str) -> None:
        self.delivered.append((chat_id, path, name))


PAIRED_REPLY = "Ulandi. Endi shu chat bilan ishlashingiz mumkin."
REFUSED_REPLY = "Kod noto'g'ri."


class FakePairing:
    """Stands in for safety.pairing.Pairing. A successful redeem pins the owner.

    Replies are the texts Pairing hands back for the chat, which the poll loop
    sends unchanged. ``refusal`` lets a test pick a different refusal reply.
    """

    def __init__(
        self,
        owner: tuple[int, int] | None = None,
        redeem_ok: bool = False,
        refusal: str = REFUSED_REPLY,
    ) -> None:
        self._owner = owner
        self.redeem_ok = redeem_ok
        self.refusal = refusal
        self.redeemed: list[tuple] = []

    def owner(self) -> tuple[int, int] | None:
        return self._owner

    def redeem(self, code: str, from_id: int, chat_id: int, chat_type: str, now: float | None = None) -> tuple[bool, str]:
        self.redeemed.append((code, from_id, chat_id, chat_type))
        if self.redeem_ok:
            self._owner = (from_id, chat_id)
            return True, PAIRED_REPLY
        return False, self.refusal


class FakeStopEvent:
    """A threading.Event stand-in. wait() records each pause and returns True once stopped."""

    def __init__(self, stop_after_waits: int | None = None, on_wait: Callable[[float], None] | None = None) -> None:
        self.waits: list[float] = []
        self._set = False
        self._stop_after = stop_after_waits
        self._on_wait = on_wait

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(float(timeout or 0.0))
        if self._on_wait is not None:
            self._on_wait(float(timeout or 0.0))
        if self._stop_after is not None and len(self.waits) >= self._stop_after:
            self._set = True
        return self._set


class FakeBudget:
    def __init__(self, verdict: Verdict | None = None) -> None:
        self.verdict = verdict or Verdict.allow()
        self.calls: list[tuple[int, int]] = []

    def admit_bytes(self, n: int, chat_id: int) -> Verdict:
        self.calls.append((n, chat_id))
        return self.verdict
