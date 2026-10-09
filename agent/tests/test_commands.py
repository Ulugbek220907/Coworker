"""Command parsing: which words are commands, and what follows them."""
from __future__ import annotations

import pytest

from coworker.transport.commands import COMMANDS, command_word, parse


def test_the_command_set_is_the_one_the_architecture_lists():
    assert COMMANDS == {
        "start", "help", "status", "stop", "panic", "forget", "reset",
        "facts", "jobs", "reminders", "audit",
    }


@pytest.mark.parametrize("text, expected", [
    ("", ("", "")),
    ("   ", ("", "")),
    (None, ("", "")),
    ("/status", ("status", "")),
    ("/STATUS", ("status", "")),
    ("  /status  ", ("status", "")),
    ("/status@CoworkerBot", ("status", "")),
    ("/forget now", ("forget", "now")),
    ("/panic   now please ", ("panic", "now please")),
    ("/help", ("help", "")),
    ("/audit 20", ("audit", "20")),
    ("/start", ("start", "")),
    ("/reminders", ("reminders", "")),
    ("/resume", ("", "/resume")),
    ("/connect 123456", ("", "/connect 123456")),
    ("/unknowncmd x", ("", "/unknowncmd x")),
    ("/", ("", "/")),
    ("hello /status", ("", "hello /status")),
    ("status", ("", "status")),
    ("zavod bilan shartnoma kerak", ("", "zavod bilan shartnoma kerak")),
])
def test_parse_returns_a_known_command_or_the_text_unchanged(text, expected):
    assert parse(text) == expected


@pytest.mark.parametrize("text, word", [
    ("/status", "status"),
    ("/Status@Bot extra", "status"),
    ("status", ""),
    ("", ""),
    (None, ""),
    ("/", ""),
])
def test_command_word_strips_the_slash_and_the_bot_suffix(text, word):
    assert command_word(text) == word
