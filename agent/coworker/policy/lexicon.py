"""Classify control labels (buttons, menu items, links) by the risk they carry.

A label such as "Delete", "Удалить" or "O'chirish" decides whether a click needs
the owner's tap. Matching is on folded text (``fold_label``) with word boundaries,
so "payroll" does not count as "pay" and "sender" does not count as "send". The
fold also reads "Place-order", a word with a zero-width character inside it, and
a Cyrillic letter inside a Latin word as the words they look like on screen.

The lists are deliberately broad: a false CONFIRM costs one tap, a false ALLOW
can cost a payment or a deleted document. Financial and credential labels are
the exception - they are checked against the tier rules, not the text.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

from ..core.types import Tier, normalize_text

# Invisible characters (zero-width spaces, soft hyphens) and accent marks are dropped
# before matching, so a word with one of them inside reads as the plain word.
_DROP_CATEGORIES = frozenset({"Cf", "Mn", "Me"})
# Dashes, connectors and punctuation that people read as a space. The minus sign
# is a math symbol, so it is listed by hand.
_BREAK_CATEGORIES = frozenset({"Pd", "Pc"})
_BREAK_CHARS = frozenset("\u2212/|\u00b7\u2022+.")
# Cyrillic and Greek letters that look like Latin ones. Both the label and the
# patterns pass through the same map, so the Cyrillic patterns still match.
_CONFUSABLES = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "һ": "h", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ӏ": "l",
    "ο": "o", "ρ": "p", "ι": "i", "χ": "x", "α": "a",
    "ν": "v", "κ": "k",
}


def fold_label(label: str) -> str:
    """The comparable form of a control label, shared by the patterns and the labels.

    A label is judged by what a person reads on screen. Invisible characters
    and accents are dropped, dashes and underscores become word breaks, and
    lookalike letters from other alphabets become their Latin twins. Matching
    therefore does not depend on which spelling of a word the control carries.
    """
    pieces: list[str] = []
    for char in unicodedata.normalize("NFKD", label or "").casefold():
        category = unicodedata.category(char)
        if category in _DROP_CATEGORIES:
            continue
        if category in _BREAK_CATEGORIES or char in _BREAK_CHARS:
            pieces.append(" ")
        else:
            pieces.append(_CONFUSABLES.get(char, char))
    return normalize_text("".join(pieces))


# Patterns are matched against fold_label(label). Apostrophes are already
# normalised to a plain quote, so one spelling covers all the variants.
_PATTERNS: dict[Tier, tuple[str, ...]] = {
    Tier.FINANCIAL: (
        r"pay", r"payment", r"pay now", r"checkout", r"check out", r"buy", r"buy now",
        r"purchase", r"place order", r"order now", r"confirm order", r"transfer", r"withdraw",
        r"deposit", r"top up", r"subscribe", r"add to cart",
        r"to'lov", r"to'lash", r"sotib ol", r"xarid qil", r"buyurtma ber", r"pul o'tkaz",
        r"оплат\w*", r"купит\w*", r"покупк\w*", r"заказат\w*", r"перевод\w*", r"вывод средств",
        r"пополн\w*", r"подписат\w*",
    ),
    Tier.CREDENTIAL: (
        r"sign in", r"log in", r"login", r"sign up", r"register", r"create account",
        r"forgot password", r"password", r"kirish", r"ro'yxatdan o'tish", r"parol",
        r"войти", r"вход", r"регистрац\w*", r"пароль", r"зарегистрир\w*",
    ),
    Tier.DESTRUCTIVE: (
        r"delete", r"remove", r"erase", r"discard", r"empty trash", r"uninstall", r"format",
        r"clear all", r"reset", r"overwrite", r"replace file", r"o'chirish", r"o'chir",
        r"olib tashla", r"yo'q qil", r"tozalash", r"удалит\w*", r"удален\w*", r"стереть",
        r"очистит\w*", r"сброс\w*", r"форматир\w*", r"перезаписат\w*",
    ),
    Tier.OUTBOUND: (
        r"send", r"submit", r"post", r"publish", r"share", r"reply", r"tweet", r"comment",
        r"forward", r"invite", r"yubor\w*", r"jo'nat\w*", r"chop et", r"ulash",
        r"отправ\w*", r"опублик\w*", r"поделит\w*", r"ответит\w*", r"переслат\w*",
        r"комментир\w*",
    ),
    Tier.SYSTEM_CHANGE: (
        r"shut ?down", r"restart", r"reboot", r"sleep", r"log ?off", r"sign ?out",
        r"install", r"update now", r"turn off", r"disable", r"enable", r"lock",
        r"o'chirib qo'y", r"qayta yuklash", r"o'rnat\w*", r"yangila\w*",
        r"выключ\w*", r"перезагруз\w*", r"установ\w*", r"отключ\w*", r"включ\w*",
    ),
}

# Priority when a label matches several tiers: money first, then secrets.
_PRIORITY = (
    Tier.FINANCIAL,
    Tier.CREDENTIAL,
    Tier.DESTRUCTIVE,
    Tier.OUTBOUND,
    Tier.SYSTEM_CHANGE,
)

def _compile(patterns: tuple[str, ...]) -> re.Pattern:
    # Longest first, so "pay now" wins over "pay". The lookarounds stop a match
    # inside a longer word; apostrophes count as part of a word. The patterns go
    # through the same fold as the labels, so both sides spell letters alike.
    body = "|".join(sorted((fold_label(p) for p in patterns), key=len, reverse=True))
    return re.compile(r"(?<![\w'])(?:" + body + r")(?![\w'])")


_COMPILED: dict[Tier, re.Pattern] = {tier: _compile(pats) for tier, pats in _PATTERNS.items()}


def classify_label(label: str) -> Optional[Tier]:
    """The strictest tier a control label implies, or None for an ordinary control.

    An empty label returns None; callers that act on unlabelled controls must
    treat that as CONFIRM themselves.
    """
    text = fold_label(label)
    if not text:
        return None
    for tier in _PRIORITY:
        if _COMPILED[tier].search(text):
            return tier
    return None


def is_money_label(label: str) -> bool:
    return classify_label(label) == Tier.FINANCIAL


def is_secret_label(label: str) -> bool:
    return classify_label(label) == Tier.CREDENTIAL


def matched_words(label: str) -> list[str]:
    """For audit lines and CONFIRM cards: which words made the label risky."""
    text = fold_label(label)
    found: list[str] = []
    for tier in _PRIORITY:
        found.extend(m.group(0) for m in _COMPILED[tier].finditer(text))
    return found
