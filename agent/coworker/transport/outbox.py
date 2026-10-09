"""Everything Coworker sends to Telegram, with the send rules in one place.

Two rules hold whatever the caller asks for. A message goes only to the
owner's chat, the chat id pinned at pairing; any other chat is refused before
the network is touched. And long text is split at CHUNK characters, because
Telegram rejects a message over 4096 units. Buttons go under the last chunk,
where the question ends.

A picture small enough for Telegram's photo limit is sent as a photo, so it
previews inline on the phone. If Telegram refuses the photo, the same file is
sent again as a document, so the owner still receives it.

A chat has at most one question that can still be answered by tap: the newest.
Its options are kept under the Telegram message that carries its buttons, and a
tap resolves only when its message is that question. An earlier question, or one
already answered, can no longer start a turn.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from ..core.types import Provenance

if TYPE_CHECKING:  # pragma: no cover
    from ..store.db import Store
    from .bot import BotApi

log = logging.getLogger("transport.outbox")

# Written by safety.pairing when the owner is pinned.
OWNER_CHAT_KEY = "owner_chat_id"

CHUNK = 3500                        # characters; Telegram's hard limit is 4096 units
MAX_DOCUMENT_BYTES = 45 * 1024 * 1024  # bot uploads are capped at 50 MB; keep headroom
MAX_PHOTO_BYTES = 10 * 1024 * 1024     # Telegram refuses larger photos
PHOTO_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
MAX_OPTIONS = 6
OPTION_LABEL_MAX = 60


def split_text(text: str, limit: int = CHUNK) -> list[str]:
    """Split on line boundaries; a line longer than the limit is cut hard.

    Telegram refuses an empty message, so an empty text becomes a middle dot.
    The parts joined together give back the original text exactly.
    """
    text = text or "·"
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > limit:
            if buf:
                parts.append(buf)
                buf = ""
            while len(line) > limit:
                parts.append(line[:limit])
                line = line[limit:]
        buf += line
    if buf:
        parts.append(buf)
    return parts


def options_key(chat_id: int) -> str:
    """kv key of the question in a chat that can still be answered by tap; empty when there is none."""
    return f"ask_options:{chat_id}"


def option_rows(options: list[str]) -> list[list[dict]]:
    """One option per row, so long file names stay readable.

    The callback carries only the option's index. The question's message id,
    which every tap reports, selects the list the index refers to.
    """
    rows: list[list[dict]] = []
    for i, option in enumerate(options[:MAX_OPTIONS]):
        label = option if len(option) <= OPTION_LABEL_MAX else option[: OPTION_LABEL_MAX - 3] + "..."
        rows.append([{"text": label, "callback_data": f"opt:{i}"}])
    return rows


def option_body(question: str, options: list[str]) -> str:
    """The question text. A cut label hides part of its option, so then every full option is listed.

    The owner must see the whole text a tap will run; a label cut at 57 characters
    is not a record of what was approved.
    """
    if all(len(option) <= OPTION_LABEL_MAX for option in options):
        return question
    listed = "\n".join(f"{i}. {option}" for i, option in enumerate(options, 1))
    return f"{question}\n\n{listed}"


def _message_id(result: dict) -> int | None:
    sent = result.get("result")
    value = sent.get("message_id") if isinstance(sent, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class Outbox:
    """Sends to the owner's private chat. Every method returns a result dict.

    A refusal has ``ok: False`` and ``code`` set to ``not_configured`` (no
    owner paired yet) or ``not_owner`` (the target is another chat). A failed
    send returns the Telegram result unchanged.
    """

    def __init__(self, api: "BotApi", store: "Store") -> None:
        self._api = api
        self._store = store

    def owner_chat(self) -> int | None:
        raw = self._store.kv_get(OWNER_CHAT_KEY)
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def text(self, chat_id: int, text: str, buttons: list[list[dict]] | None = None) -> dict:
        refused = self._refuse(chat_id)
        if refused:
            return refused
        parts = split_text(text)
        result: dict[str, Any] = {"ok": True}
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            result = self._api.send_message(chat_id, part, buttons=buttons if last else None)
            if not result.get("ok"):
                return result  # a half-sent message is reported, not hidden
        return result

    def ask(self, chat_id: int, question: str, options: list[str],
            provenance: int = int(Provenance.CONTENT)) -> dict:
        """Send a question with one button per option. It becomes the chat's only answerable question.

        ``provenance`` is the provenance of the turn that wrote the question. A tap
        runs the option with that provenance, so text the model took from a page
        stays untrusted when the owner taps it. The default is the untrusted level:
        a caller that does not say what it knows gets the stricter rules.
        """
        kept = [str(o) for o in options[:MAX_OPTIONS]]
        result = self.text(chat_id, option_body(question, kept), buttons=option_rows(kept))
        if not result.get("ok"):
            return result
        self.retire_asks(chat_id)
        message_id = _message_id(result)
        if message_id is not None:
            self._store.kv_set(options_key(chat_id), {
                "message_id": message_id, "options": kept, "provenance": int(provenance),
            })
        return result

    def claim_ask(self, chat_id: int, message_id: int | None, index: int) -> tuple[str, int] | None:
        """The option a tap picked, with the provenance of its question. The question is then closed.

        Returns None, and claims nothing, when the message is not the chat's answerable
        question or the index is out of range. A second tap on the same question finds nothing.
        """
        live = self._live(chat_id)
        if live is None or message_id != live["message_id"] or not 0 <= index < len(live["options"]):
            return None
        self._store.kv_set(options_key(chat_id), {})
        return str(live["options"][index]), int(live["provenance"])

    def retire_asks(self, chat_id: int) -> None:
        """Remove the buttons of the chat's answerable question, if any, and forget its options.

        Called when a newer question is sent and when the owner answers in text, so
        an old button cannot start a turn of its own.
        """
        live = self._live(chat_id)
        if live is None:
            return
        self._api.edit_reply_markup(chat_id, int(live["message_id"]))
        self._store.kv_set(options_key(chat_id), {})

    def edit_markup(self, chat_id: int, message_id: int) -> dict:
        """Remove the buttons from one message in the owner's chat.

        The same owner check as a send applies. Removing buttons changes what the
        owner sees, so it is refused for any other chat before the network is touched.
        """
        refused = self._refuse(chat_id)
        if refused:
            return refused
        return self._api.edit_reply_markup(chat_id, int(message_id))

    def document(self, chat_id: int, path: str, caption: str = "") -> dict:
        refused = self._refuse(chat_id)
        if refused:
            return refused
        if not os.path.isfile(path):
            return {"ok": False, "code": "file_missing", "description": "file not found"}
        size = os.path.getsize(path)
        if size > MAX_DOCUMENT_BYTES:
            return {"ok": False, "code": "too_large", "description": f"{size} bytes is over the limit"}

        if path.lower().endswith(PHOTO_SUFFIXES) and size <= MAX_PHOTO_BYTES:
            result = self._api.send_photo(chat_id, path, caption)
            if result.get("ok"):
                return result
            log.info("photo refused by Telegram, sending %s as a document", os.path.basename(path))
        return self._api.send_document(chat_id, path, caption)

    def notify(self, text: str) -> dict:
        chat_id = self.owner_chat()
        if chat_id is None:
            return {"ok": False, "code": "not_configured", "description": "no owner is paired"}
        return self.text(chat_id, text)

    def _live(self, chat_id: int) -> dict | None:
        raw = self._store.kv_get(options_key(chat_id))
        return raw if isinstance(raw, dict) and raw.get("message_id") is not None else None

    def _refuse(self, chat_id: int) -> dict | None:
        owner = self.owner_chat()
        if owner is None:
            return {"ok": False, "code": "not_configured", "description": "no owner is paired"}
        if chat_id != owner:
            log.warning("refused a send to a chat that is not the owner's")
            return {"ok": False, "code": "not_owner", "description": "target is not the owner's chat"}
        return None
