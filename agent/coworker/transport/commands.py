"""Telegram slash commands: the words the owner can type that Coworker acts on.

Only the names in COMMANDS are commands. Anything else, including /resume and
/connect after pairing, is returned as plain text for the model. /resume is
local-only by design: a remote message must never be able to clear panic.
"""
from __future__ import annotations

COMMANDS = frozenset({
    "start", "help", "status", "stop", "panic", "forget", "reset",
    "facts", "jobs", "reminders", "audit",
})


def command_word(text: str) -> str:
    """The leading command without slash or @bot suffix, lowercased; "" for plain text.

    Telegram appends @botname to commands in group chats, so "/status@Bot" is
    the same command as "/status". Only the first word is inspected.
    """
    words = (text or "").split(maxsplit=1)
    if not words or not words[0].startswith("/"):
        return ""
    return words[0][1:].split("@", 1)[0].lower()


def parse(text: str) -> tuple[str, str]:
    """Return (name, args) for a known command, or ("", text) for anything else.

    The arguments are the rest of the message, stripped. For a known command
    the name is lowercase, so "/STATUS" and "/status" are the same.
    """
    stripped = (text or "").strip()
    name = command_word(stripped)
    if name not in COMMANDS:
        return "", stripped
    words = stripped.split(maxsplit=1)
    args = words[1].strip() if len(words) > 1 else ""
    return name, args
