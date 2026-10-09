"""Prohibited content: Luhn-checked card numbers, key shapes, nested arguments and redaction."""
from __future__ import annotations

import pytest

from coworker.policy.prohibited import luhn_valid, redact, scan_args, scan_text

# Published test numbers. Each one is checked against luhn_valid below, so a typo here fails loudly.
VALID_CARDS = [
    "4111111111111111",        # Visa test number
    "5555555555554444",        # Mastercard test number
    "378282246310005",         # 15-digit American Express test number
    "6011111111111117",        # Discover test number
]
INVALID_CARD = "4111111111111112"


def test_fixture_numbers_have_the_luhn_property_they_claim():
    assert all(luhn_valid(number) for number in VALID_CARDS)
    assert not luhn_valid(INVALID_CARD)


@pytest.mark.parametrize("number", VALID_CARDS)
def test_luhn_valid_card_numbers_are_refused(number):
    assert scan_args({"text": number}) == "prohibited_card"


@pytest.mark.parametrize("grouped", [
    "4111 1111 1111 1111",
    "4111-1111-1111-1111",
    "4111 1111-1111 1111",
])
def test_grouping_with_spaces_or_dashes_does_not_hide_a_card(grouped):
    assert scan_args({"text": grouped}) == "prohibited_card"


def test_a_card_inside_a_sentence_is_refused():
    assert scan_args({"text": "pay with card 4111111111111111 please"}) == "prohibited_card"


def test_a_card_typed_as_a_json_number_is_refused():
    assert scan_args({"amount": 4111111111111111}) == "prohibited_card"


def test_a_luhn_invalid_run_is_not_a_card():
    assert scan_args({"text": INVALID_CARD}) is None
    assert scan_args({"text": "4111 1111 1111 1112"}) is None


def test_a_run_longer_than_19_digits_is_not_a_card():
    assert scan_args({"text": "41111111111111111111"}) is None


def test_a_short_run_is_not_a_card():
    assert scan_args({"text": "401288888888"}) is None  # 12 digits, Luhn-valid but too short


def test_nested_arguments_are_scanned():
    args = {"items": [{"note": "ok"}, {"deep": {"value": "4111111111111111"}}]}
    assert scan_args(args) == "prohibited_card"


@pytest.mark.parametrize("value", [
    "sk-proj-AbCdEf0123456789xyz",
    "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
    "AKIAIOSFODNN7EXAMPLE",
    "xoxb-1234567890-abcdefghij",
    "xoxp-1234567890-abcdefghij",
    "-----BEGIN RSA PRIVATE KEY-----",
    "Authorization: Bearer abcdefghijklmnop1234",
])
def test_key_shapes_are_refused(value):
    assert scan_args({"text": value}) == "prohibited_secret"


@pytest.mark.parametrize("value", [
    "risk-management-strategy-2024",   # "sk-" inside a word
    "task-list-items",
    "sk-short",                        # body under 16 characters
    "Bearer abc",                      # bearer token under 16 characters
    "AKIA",                            # prefix without a body
    "xoxb-1",
    "the bearer of good news",
])
def test_ordinary_text_is_not_a_key(value):
    assert scan_args({"text": value}) is None


def test_card_is_reported_before_secret_when_both_are_present():
    args = {"a": "sk-abcdefghijklmnop1234", "b": "4111111111111111"}
    assert scan_args(args) == "prohibited_card"


def test_clean_arguments_return_none():
    assert scan_args({"path": "C:\\notes.txt", "limit": 20, "flag": True, "items": []}) is None


def test_redact_replaces_a_card_and_keeps_its_length():
    out = redact("card 4111 1111 1111 1111 end")
    assert "4111" not in out
    assert out == "card [redacted:card:19] end"


def test_redact_leaves_a_luhn_invalid_run_untouched():
    assert redact("order 4111111111111112") == "order 4111111111111112"


def test_redact_replaces_a_key_with_a_length_marker():
    key = "sk-abcdefghijklmnop1234"
    out = redact(f"token {key} used")
    assert key not in out
    assert out == f"token [redacted:secret:{len(key)}] used"


def test_redact_removes_a_whole_pem_block_not_only_its_header():
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----"
    out = redact(f"before\n{pem}\nafter")
    assert "b3BlbnNzaC" not in out
    assert out.startswith("before\n[redacted:secret:")
    assert out.endswith("\nafter")


def test_redact_is_a_no_op_on_clean_text():
    assert redact("nothing to see here") == "nothing to see here"


def test_luhn_checksum_matches_the_standard_examples():
    assert luhn_valid("79927398713")
    assert not luhn_valid("79927398710")


# ------------------------------------------- card digits next to other digits or odd separators


@pytest.mark.parametrize("text", [
    "4111 1111 1111 1111 123",               # card followed by the CVV
    "4111111111111111 123",                  # the same, unseparated card
    "1 4111 1111 1111 1111",                 # one digit before the card
    "4111 1111 1111 1111, 123",              # a comma after the card
    "4111\u00a01111\u00a01111\u00a01111",    # no-break spaces
    "4111\u20081111\u20081111\u20081111",    # punctuation spaces
    "4111\u20131111\u20131111\u20131111",    # en dashes
    "4111\u20111111\u20111111\u20111111",    # non-breaking hyphens
    "4111\u2212 1111\u2212 1111\u2212 1111",  # minus signs
    "4111.1111.1111.1111",                   # dots
    "4111/1111/1111/1111",                   # slashes
    "4111  1111  1111  1111",                # double spaces
    "4111\u200b1111 1111 1111",              # zero-width space between two digits
    "card: 4111 1111 1111 1111.",
])
def test_a_card_is_found_with_extra_digits_or_any_separator(text):
    assert scan_args({"text": text}) == "prohibited_card"


def test_a_card_hidden_in_a_longer_grouped_number_is_found():
    assert scan_args({"text": "ref 12 4111 1111 1111 1111 9"}) == "prohibited_card"


def test_separators_that_are_not_digit_joins_do_not_join_unrelated_numbers():
    assert scan_args({"text": "qty 41 and 11 11 11 11 11"}) is None


def test_a_card_split_by_a_line_break_is_not_joined_across_lines():
    assert scan_args({"text": "4111 1111\n1111 1111"}) is None


def test_redact_removes_the_card_but_not_the_cvv_that_follows_it():
    assert redact("4111 1111 1111 1111 123") == "[redacted:card:19] 123"


def test_scan_text_reports_the_same_codes_as_scan_args():
    assert scan_text("4111 1111 1111 1111") == "prohibited_card"
    assert scan_text("sk-abcdefghijklmnop1234") == "prohibited_secret"
    assert scan_text("nothing here") is None


def test_a_card_written_with_percent_escapes_is_refused():
    """A card with %20 between groups is a card once decoded; the raw text alone looked clean."""
    from coworker.policy import prohibited

    assert prohibited.scan_args({"text": "4111%201111%201111%201111"}) == "prohibited_card"
    assert prohibited.scan_args({"text": "4111+1111+1111+1111"}) == "prohibited_card"
    assert prohibited.scan_args({"text": "hello%20world"}) is None
