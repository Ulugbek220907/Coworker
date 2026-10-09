"""The outbox's button-removal helper: it obeys the same owner check as a send.

Removing the buttons of a card changes what the owner sees, so it is held to the rule
every outbound call follows: the owner's chat only, checked before the network is touched.
"""
from __future__ import annotations

import pytest

from coworker.transport.outbox import OWNER_CHAT_KEY, Outbox
from fakes.telegram_fake import FakeBotApi

OWNER_CHAT = 4242


class EditApi(FakeBotApi):
    def edit_reply_markup(self, chat_id, message_id):
        self.calls.append(("edit_reply_markup", {"chat_id": chat_id, "message_id": message_id}))
        return {"ok": True}


@pytest.fixture
def paired(store):
    store.kv_set(OWNER_CHAT_KEY, OWNER_CHAT)
    return store


def test_the_buttons_of_a_message_in_the_owner_chat_are_removed(paired):
    api = EditApi()

    result = Outbox(api, paired).edit_markup(OWNER_CHAT, 101)

    assert result == {"ok": True}
    assert api.calls == [("edit_reply_markup", {"chat_id": OWNER_CHAT, "message_id": 101})]


def test_another_chat_is_refused_before_the_network_is_touched(paired):
    api = EditApi()

    result = Outbox(api, paired).edit_markup(9999, 101)

    assert result["ok"] is False
    assert result["code"] == "not_owner"
    assert api.calls == []


def test_before_pairing_no_buttons_are_touched(store):
    api = EditApi()

    result = Outbox(api, store).edit_markup(OWNER_CHAT, 101)

    assert result["ok"] is False
    assert result["code"] == "not_configured"
    assert api.calls == []


def test_the_message_id_reaches_the_api_as_an_integer(paired):
    api = EditApi()

    Outbox(api, paired).edit_markup(OWNER_CHAT, "101")

    assert api.calls == [("edit_reply_markup", {"chat_id": OWNER_CHAT, "message_id": 101})]
