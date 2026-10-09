"""Control-label classification: word boundaries, languages, apostrophes and priority."""
from __future__ import annotations

import pytest

from coworker.core.types import Tier
from coworker.policy.lexicon import classify_label, is_money_label, is_secret_label, matched_words


@pytest.mark.parametrize("label, tier", [
    ("Pay now", Tier.FINANCIAL),
    ("Checkout", Tier.FINANCIAL),
    ("Place order", Tier.FINANCIAL),
    ("Add to cart", Tier.FINANCIAL),
    ("Оплатить", Tier.FINANCIAL),
    ("Купить", Tier.FINANCIAL),
    ("To'lov", Tier.FINANCIAL),
    ("Sign in", Tier.CREDENTIAL),
    ("Log in", Tier.CREDENTIAL),
    ("Forgot password", Tier.CREDENTIAL),
    ("Войти", Tier.CREDENTIAL),
    ("Kirish", Tier.CREDENTIAL),
    ("Delete", Tier.DESTRUCTIVE),
    ("Empty trash", Tier.DESTRUCTIVE),
    ("Удалить", Tier.DESTRUCTIVE),
    ("O'chirish", Tier.DESTRUCTIVE),
    ("Send", Tier.OUTBOUND),
    ("Submit", Tier.OUTBOUND),
    ("Отправить", Tier.OUTBOUND),
    ("Yuborish", Tier.OUTBOUND),
    ("Shut down", Tier.SYSTEM_CHANGE),
    ("Log off", Tier.SYSTEM_CHANGE),
    ("Turn off", Tier.SYSTEM_CHANGE),
    ("Выключить", Tier.SYSTEM_CHANGE),
])
def test_risky_labels_map_to_their_tier(label, tier):
    assert classify_label(label) == tier


@pytest.mark.parametrize("label", ["Save", "Open", "Cancel", "Next", "Close", "OK", "Скачать"])
def test_ordinary_labels_are_not_classified(label):
    assert classify_label(label) is None


@pytest.mark.parametrize("label", ["Payroll", "sender", "Postpone", "Block", "Reposition", "Lockdown"])
def test_words_that_only_contain_a_risky_word_are_not_matched(label):
    assert classify_label(label) is None


def test_a_curly_apostrophe_matches_the_straight_form():
    assert classify_label("O’chirish") == Tier.DESTRUCTIVE
    assert classify_label("To’lov") == Tier.FINANCIAL


def test_case_and_surrounding_whitespace_do_not_matter():
    assert classify_label("   PAY   NOW  ") == Tier.FINANCIAL


def test_empty_and_blank_labels_return_none():
    assert classify_label("") is None
    assert classify_label("   ") is None


def test_money_outranks_secrets_and_destruction():
    assert classify_label("Log in and pay") == Tier.FINANCIAL
    assert classify_label("Pay and delete") == Tier.FINANCIAL


def test_secrets_outrank_destruction_and_outbound():
    assert classify_label("Sign in and send") == Tier.CREDENTIAL


def test_destruction_outranks_outbound_and_system_change():
    assert classify_label("Delete and send") == Tier.DESTRUCTIVE


def test_outbound_outranks_system_change():
    assert classify_label("Send and shut down") == Tier.OUTBOUND


def test_is_money_and_is_secret_follow_the_classification():
    assert is_money_label("Buy now")
    assert not is_money_label("Send")
    assert is_secret_label("Sign up")
    assert not is_secret_label("Pay now")


def test_matched_words_lists_each_risky_word_found():
    assert sorted(matched_words("Pay and send")) == ["pay", "send"]


def test_matched_words_is_empty_for_an_ordinary_label():
    assert matched_words("Open file") == []


# ------------------------------------------------- spelling tricks that must not hide a label


@pytest.mark.parametrize("label, tier", [
    ("Place-order", Tier.FINANCIAL),          # hyphen for a space
    ("Place_order", Tier.FINANCIAL),          # underscore for a space
    ("Check-out", Tier.FINANCIAL),
    ("Add-to-cart", Tier.FINANCIAL),
    ("Sign-in", Tier.CREDENTIAL),
    ("Log_in", Tier.CREDENTIAL),
    ("Pay\u2010now", Tier.FINANCIAL),         # U+2010 hyphen
    ("Pay\u2013now", Tier.FINANCIAL),         # en dash
    ("Pay\u2014now", Tier.FINANCIAL),         # em dash
    ("Pay\u2212now", Tier.FINANCIAL),         # minus sign
    ("Pay\ufe63now", Tier.FINANCIAL),         # small hyphen-minus
    ("Pay\uff0dnow", Tier.FINANCIAL),         # full-width hyphen-minus
    ("Pa\u200byment", Tier.FINANCIAL),        # zero-width space inside the word
    ("Pay\u00admen\u200dt", Tier.FINANCIAL),  # soft hyphen and zero-width joiner
    ("Pa\u0301y", Tier.FINANCIAL),            # combining accent on a Latin letter
    ("Pl\u0430ce order", Tier.FINANCIAL),     # Cyrillic a inside a Latin word
    ("\u0420l\u0430ce \u043erder", Tier.FINANCIAL),  # Cyrillic p, a and o
    ("Ch\u0435ck-out", Tier.FINANCIAL),       # Cyrillic e
    ("\u041e\u043f\u043ba\u0442\u0438\u0442\u044c", Tier.FINANCIAL),  # Cyrillic word with one Latin a
    ("Del\u0435te", Tier.DESTRUCTIVE),        # Cyrillic e inside Delete
    ("Sen\u0301d", Tier.OUTBOUND),
])
def test_separator_and_lookalike_tricks_do_not_hide_a_risky_label(label, tier):
    assert classify_label(label) == tier


def test_a_hyphenated_ordinary_label_stays_ordinary():
    assert classify_label("Open-file") is None


def test_a_label_with_only_invisible_characters_is_ordinary():
    assert classify_label("\u200b\u200c\u200d") is None


def test_matched_words_sees_through_the_same_tricks():
    assert matched_words("Place\u200b-order") == ["place order"]
