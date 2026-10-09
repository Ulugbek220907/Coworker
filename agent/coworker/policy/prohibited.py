"""Content that must never be typed, sent or logged: payment cards and key material.

The scan runs over every argument of every tool call (kernel step 6) and over
text that is about to be logged or copied to the clipboard. It looks only for
shapes that are unambiguous. A card is a run of 13-19 digits that passes the
Luhn check, so a random digit string rarely matches. The digits may be grouped
with any kind of space, dash, dot or slash, and the scan tries every run of
consecutive groups, so a card typed with its CVV, or with a stray digit in
front, is still found. A key is a vendor prefix followed by a body of realistic
length, so ordinary words such as "task-list" or "disk-usage" do not match.

Refusals name the kind of value that was found and never repeat it.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, Optional
from urllib.parse import unquote_plus

_MIN_CARD_DIGITS = 13
_MAX_CARD_DIGITS = 19

# Characters that may sit between two groups of one card number: horizontal
# whitespace of any width, dashes of every kind (the minus sign included), dots,
# slashes, and the format characters that can hide between two digits. A line
# break ends the number, so a list of short numbers is not read as one card.
_SEPARATOR = r"(?:[^\S\r\n]|[-\u00ad\u200b-\u200f\u2010-\u2015\u2060\u2212\ufe58\ufe63\ufeff\uff0d./])"
_CHAIN = re.compile(r"\d+(?:" + _SEPARATOR + r"+\d+)*")
_GROUP = re.compile(r"\d+")

# The lookbehind keeps "sk-" from matching inside "task-" or "disk-". Bodies
# shorter than 16 characters are not treated as keys.
_KEY_SHAPES = (
    re.compile(r"(?<![A-Za-z0-9])(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])"),
    re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bBearer [^\s]{16,}", re.IGNORECASE),
)
_PEM_HEADER = re.compile(r"-----BEGIN")
# Redaction removes a whole PEM block, not only its header line, so the key body
# never reaches a log.
_PEM_BLOCK = re.compile(r"-----BEGIN[\s\S]*?(?:-----END[^-]*-----|\Z)")


def luhn_valid(digits: str) -> bool:
    """The Luhn checksum over a string of decimal digits."""
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def scan_args(args: dict) -> Optional[str]:
    """``prohibited_card`` or ``prohibited_secret`` when any argument holds one, else None.

    Integers are scanned as their decimal text, because a model can send a card
    number as a JSON number.
    """
    raw = list(_strings(args))
    # A card written with %20 or + between groups reads as a card only once decoded.
    decoded = [unquote_plus(s) for s in raw if "%" in s or "+" in s]
    return _first_refusal(raw + decoded)


def scan_text(text: str) -> Optional[str]:
    """The same answer as ``scan_args`` for one piece of text, for callers that join text themselves."""
    return _first_refusal([text])


def redact(text: str) -> str:
    """Replace every card and key shape with a marker that records only the length."""
    text = _PEM_BLOCK.sub(lambda m: _marker("secret", m.group(0)), text)
    for shape in _KEY_SHAPES:
        text = shape.sub(lambda m: _marker("secret", m.group(0)), text)
    for start, end in reversed(_card_spans(text)):
        text = text[:start] + _marker("card", text[start:end]) + text[end:]
    return text


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, int):
        yield str(value)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _first_refusal(texts: list[str]) -> Optional[str]:
    # A card is reported before a key when both are present.
    if any(_card_spans(text) for text in texts):
        return "prohibited_card"
    if any(_has_secret(text) for text in texts):
        return "prohibited_secret"
    return None


def _card_spans(text: str) -> list[tuple[int, int]]:
    """Spans of text that hold a Luhn-valid card, from the first digit to the last.

    Every run of consecutive digit groups is tried, from each starting group, up
    to 19 digits, so a card is found whether it is the whole chain or only part
    of it (a card followed by its CVV, or preceded by a stray digit). A single
    digit run longer than 19 digits is never a card: testing its windows would
    refuse about one identifier in ten by chance.
    """
    spans: list[tuple[int, int]] = []
    for chain in _CHAIN.finditer(text):
        base = chain.start()
        groups = [
            (base + m.start(), base + m.end(), m.group(0))
            for m in _GROUP.finditer(chain.group(0))
        ]
        for first in range(len(groups)):
            digits = ""
            for last in range(first, len(groups)):
                digits += groups[last][2]
                if len(digits) > _MAX_CARD_DIGITS:
                    break
                if len(digits) >= _MIN_CARD_DIGITS and luhn_valid(digits):
                    spans.append((groups[first][0], groups[last][1]))
    return _merge(spans)


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _has_secret(text: str) -> bool:
    return _PEM_HEADER.search(text) is not None or any(
        shape.search(text) is not None for shape in _KEY_SHAPES
    )


def _marker(kind: str, matched: str) -> str:
    return f"[redacted:{kind}:{len(matched)}]"
