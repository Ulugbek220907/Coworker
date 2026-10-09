"""OwnerGate: the table of who may reach Coworker, before and after pairing.

The gate is pure: it asks the pairing for the owner and answers yes or no. A
FakePairing stands in for safety.pairing, so these rules are checked without
the real pairing state.
"""
from __future__ import annotations

import pytest

from coworker.transport.gate import OwnerGate, connect_code
from fakes.telegram_fake import PAIRED_REPLY, REFUSED_REPLY, FakePairing, FakeStore

OWNER_USER = 111
OWNER_CHAT = 111


def message(*, user=OWNER_USER, chat=OWNER_CHAT, chat_type="private", text="salom") -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": user, "is_bot": False},
            "chat": {"id": chat, "type": chat_type},
            "text": text,
        },
    }


def callback(*, user=OWNER_USER, chat=OWNER_CHAT, chat_type="private", data="opt:0") -> dict:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": user},
            "data": data,
            "message": {"message_id": 9, "chat": {"id": chat, "type": chat_type}},
        },
    }


def paired_gate(owner=(OWNER_USER, OWNER_CHAT)) -> OwnerGate:
    return OwnerGate(FakeStore(), pairing=FakePairing(owner=owner))


def unpaired_gate(redeem_ok=False) -> tuple[OwnerGate, FakePairing]:
    pairing = FakePairing(owner=None, redeem_ok=redeem_ok)
    return OwnerGate(FakeStore(), pairing=pairing), pairing


# ----------------------------------------------------- paired: who gets in


@pytest.mark.parametrize("update, expected, why", [
    (message(), True, "the owner, in the owner's private chat"),
    (callback(), True, "a button tap from the owner in the owner's chat"),
    (message(user=999, chat=999), False, "a stranger in their own private chat"),
    (message(user=999), False, "a stranger using the owner's chat id"),
    (message(chat=555), False, "the owner's id in another chat"),
    (message(chat_type="group", chat=-100123), False, "the owner in a group"),
    (message(chat_type="supergroup", chat=OWNER_CHAT), False, "the owner's chat id but a supergroup type"),
    (callback(user=999, chat=999), False, "a button tap from a stranger"),
    (callback(user=999, chat=999, data="confirm:yes"), False, "a stranger's approval tap"),
    (callback(chat_type="group", chat=-100123), False, "a tap from the owner inside a group"),
    ({"update_id": 3, "edited_message": message()["message"]}, False, "an edited message, not a new one"),
    ({"update_id": 4, "channel_post": message()["message"]}, False, "a channel post"),
    ({"update_id": 5, "my_chat_member": {"chat": {"id": 1}}}, False, "a membership change"),
    ("not an update", False, "a value that is not an object"),
    (None, False, "no update at all"),
    ({"update_id": 6, "message": {"chat": {"id": OWNER_CHAT, "type": "private"}}}, False, "no sender"),
    ({"update_id": 7, "callback_query": {"id": "x", "from": {"id": OWNER_USER}}}, False, "a callback without a message"),
])
def test_the_paired_gate_admits_only_the_owner_in_the_owner_chat(update, expected, why):
    assert paired_gate().allows(update) is expected, why


def test_both_the_user_and_the_chat_must_match_the_pinned_owner():
    gate = paired_gate(owner=(OWNER_USER, 777))

    assert gate.allows(message(user=OWNER_USER, chat=777)) is True
    assert gate.allows(message(user=OWNER_USER, chat=OWNER_CHAT)) is False
    assert gate.allows(message(user=999, chat=777)) is False


def test_a_boolean_id_is_never_taken_for_a_telegram_id():
    update = message()
    update["message"]["from"]["id"] = True

    assert paired_gate(owner=(1, 1)).allows(update) is False


def test_the_gate_asks_the_pairing_each_time_so_a_new_owner_takes_effect():
    pairing = FakePairing(owner=(OWNER_USER, OWNER_CHAT))
    gate = OwnerGate(FakeStore(), pairing=pairing)
    assert gate.allows(message()) is True

    pairing._owner = (222, 222)

    assert gate.allows(message()) is False
    assert gate.paired() is True


# ------------------------------------------------- unpaired: only /connect


@pytest.mark.parametrize("update, expected, why", [
    (message(text="/connect 123456", user=999, chat=999), True, "a /connect from any private chat"),
    (message(text="/connect 123456"), True, "a /connect from the would-be owner"),
    (message(text="/CONNECT 123456"), True, "the command in capitals"),
    (message(text="/connect@CoworkerBot 123456"), True, "the command with a bot suffix"),
    (message(text="/connect 123456", chat_type="group", chat=-5), False, "a /connect inside a group"),
    (message(text="/connect"), False, "a /connect with no code"),
    (message(text="/connect 1 2"), False, "a /connect with extra words"),
    (message(text="/start"), False, "/start before pairing"),
    (message(text="salom"), False, "plain text before pairing"),
    (message(text="/resume"), False, "a local-only command"),
    (callback(data="ap:x:y:y"), False, "a button tap before pairing"),
    ({"update_id": 8, "message": {"from": {"id": 9}, "chat": {"id": 9, "type": "private"},
                                 "text": "/connect 123456"}}, True, "a /connect with a sender"),
    ({"update_id": 9, "message": {"chat": {"id": 9, "type": "private"}, "text": "/connect 123456"}},
     False, "a /connect with no sender"),
])
def test_before_pairing_only_a_private_connect_passes(update, expected, why):
    gate, _ = unpaired_gate()
    assert gate.allows(update) is expected, why


def test_a_connect_code_longer_than_the_limit_is_not_passed():
    gate, _ = unpaired_gate()

    assert gate.allows(message(text="/connect " + "9" * 40)) is False


def test_redeem_passes_the_code_and_the_sender_to_the_pairing():
    gate, pairing = unpaired_gate(redeem_ok=True)

    chat, reply, accepted = gate.redeem(message(text="/connect 654321", user=888, chat=888))

    assert pairing.redeemed == [("654321", 888, 888, "private")]
    assert (chat, accepted) == (888, True)
    assert reply == PAIRED_REPLY
    assert gate.paired() is True


def test_redeem_reports_a_refused_code_and_leaves_the_owner_unset():
    gate, pairing = unpaired_gate(redeem_ok=False)

    chat, reply, accepted = gate.redeem(message(text="/connect 000000", user=888, chat=888))

    assert (chat, accepted) == (888, False)
    assert reply == REFUSED_REPLY
    assert gate.paired() is False


def test_redeem_refuses_anything_that_is_not_a_connect():
    gate, _ = unpaired_gate()

    with pytest.raises(ValueError):
        gate.redeem(message(text="/start"))


@pytest.mark.parametrize("text, code", [
    ("/connect 123456", "123456"),
    ("  /connect   123456  ", "123456"),
    ("/connect", None),
    ("/connect a b", None),
    ("/status 123456", None),
    ("salom", None),
    ("", None),
    ("/connect " + "x" * 33, None),
])
def test_connect_code_reads_only_a_single_code_argument(text, code):
    assert connect_code(text) == code
