"""Who may talk to Coworker over Telegram: the paired owner, and nobody else.

The gate runs before an update is parsed. Trust rests on a numeric user id and
a private chat id, both pinned at pairing. Both must match: a chat id alone is
not enough, and a group the owner also belongs to is not the owner's chat.

Before pairing there is no owner, so the only update that passes is a
``/connect <code>`` message sent in a private chat. It goes to Pairing.redeem,
which owns the failure count and the lockout. Every other update is dropped.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .commands import command_word

if TYPE_CHECKING:  # pragma: no cover
    from ..safety.pairing import Pairing

PRIVATE = "private"
PAIR_CODE_MAX = 32
_TRUSTED_KINDS = ("message", "callback_query")


@dataclass(frozen=True)
class _Event:
    kind: str
    from_id: int | None
    chat_id: int | None
    chat_type: str
    text: str


def _as_int(value: Any) -> int | None:
    # bool is a subclass of int, and a JSON true is never a Telegram id.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _event(update: Any) -> _Event | None:
    """Extract the sender and chat from a message or a callback. Anything else is None."""
    if not isinstance(update, dict):
        return None
    for kind in _TRUSTED_KINDS:
        body = update.get(kind)
        if not isinstance(body, dict):
            continue
        sender = body.get("from") if isinstance(body.get("from"), dict) else {}
        if kind == "message":
            chat = body.get("chat") if isinstance(body.get("chat"), dict) else {}
            text = body.get("text") if isinstance(body.get("text"), str) else ""
        else:
            # A callback's chat is the chat of the message that carried the button.
            message = body.get("message") if isinstance(body.get("message"), dict) else {}
            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            text = ""
        return _Event(
            kind=kind,
            from_id=_as_int(sender.get("id")),
            chat_id=_as_int(chat.get("id")),
            chat_type=str(chat.get("type") or ""),
            text=text,
        )
    return None


def connect_code(text: str) -> str | None:
    """The code of a well-formed ``/connect <code>``, or None."""
    if command_word(text) != "connect":
        return None
    words = text.split()
    if len(words) != 2 or len(words[1]) > PAIR_CODE_MAX:
        return None
    return words[1]


class OwnerGate:
    """Decides whether an update may proceed. ``allows`` is pure; ``redeem`` is the one write.

    The default Pairing is built from the store, which is where it pins the
    owner. Tests pass a stand-in instead, so the rules here can be checked
    without the safety module.
    """

    def __init__(self, store: Any, pairing: "Pairing | None" = None) -> None:
        if pairing is None:
            from ..safety.pairing import Pairing

            pairing = Pairing(store)
        self._pairing = pairing

    def paired(self) -> bool:
        return self._pairing.owner() is not None

    def allows(self, update: Any) -> bool:
        event = _event(update)
        if event is None:
            return False
        owner = self._pairing.owner()
        if owner is None:
            return (
                event.kind == "message"
                and event.chat_type == PRIVATE
                and event.from_id is not None
                and event.chat_id is not None
                and connect_code(event.text) is not None
            )
        owner_user, owner_chat = owner
        return (
            event.from_id == owner_user
            and event.chat_id == owner_chat
            and event.chat_type == PRIVATE
        )

    def redeem(self, update: Any) -> tuple[int, str, bool]:
        """Pass a pre-pairing ``/connect`` to Pairing.redeem.

        Call only after allows() returned True with no owner paired. Returns the
        chat to answer, the reply Pairing wrote for it (already in Uzbek, and
        specific: a lockout reads differently from a wrong code), and whether
        the chat was paired.
        """
        event = _event(update)
        code = connect_code(event.text) if event is not None else None
        if event is None or code is None or event.from_id is None or event.chat_id is None:
            raise ValueError("redeem needs a well-formed /connect update")
        accepted, message = self._pairing.redeem(code, event.from_id, event.chat_id, PRIVATE)
        return event.chat_id, str(message), bool(accepted)
