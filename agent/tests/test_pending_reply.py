"""A typed yes or no while an approval card waits answers the card, and never starts a turn.

The owner's words choose a fixed reply and nothing else. A yes approves nothing and runs
nothing: it sends the card again, with the same buttons, so the owner taps there. A no
declines the card it answers and removes that card's buttons. Any other text is an
ordinary turn, and the card stays pending. An expired card gets its own reply.

The fake Telegram keeps what each message holds, so a test can see the buttons that
are left on the screen, not only the calls that were made.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from coworker.config import Config
from coworker.core.types import Provenance
from coworker.orchestrator.prompt import FORWARD_NOTE
from coworker.orchestrator.turn import Reply
from coworker.runtime import (
    ANSWER_NO, ANSWER_YES, EXPIRED_REPLY, YES_REPLY, Runtime, approval_answer,
)
from coworker.safety.pairing import OWNER_CHAT_KEY, OWNER_USER_KEY
from coworker.store.approval_rows import APPROVED, DENIED, PENDING
from fakes.os_fake import FakeOs
from fakes.telegram_fake import FakeBotApi

OWNER_USER = 7
OWNER_CHAT = 4242
CARD = "⚠️ Tasdiq kerak:\n"
DECLINED = "❌ Bekor qilindi. Hech narsa o'zgarmadi."


class HeldApi(FakeBotApi):
    """Gives each sent message an id, as Telegram does, and keeps what each message holds."""

    def __init__(self) -> None:
        super().__init__()
        self.next_id = 100
        self.held: dict[int, dict] = {}   # message id -> {"text", "buttons"}; buttons [] once cleared

    def send_message(self, chat_id, text, buttons=None, reply_to=None):
        self._answer("send_message", chat_id=chat_id, text=text, buttons=buttons, reply_to=reply_to)
        self.next_id += 1
        self.held[self.next_id] = {"text": text, "buttons": buttons or []}
        return {"ok": True, "result": {"message_id": self.next_id}}

    def edit_reply_markup(self, chat_id, message_id):
        self.calls.append(("edit_reply_markup", {"chat_id": chat_id, "message_id": message_id}))
        if message_id in self.held:
            self.held[message_id]["buttons"] = []
        return {"ok": True}

    def answer_callback(self, callback_id, text=""):
        self.calls.append(("answer_callback", {"callback_id": callback_id}))
        return {"ok": True}


class IdleProvider:
    dialect = "openai"
    name = "idle"

    async def chat(self, system, messages, tools, max_tokens=900):
        return SimpleNamespace(text="ok", tool_calls=[])


def make_runtime(store_path):
    api = HeldApi()
    runtime = Runtime(Config(), api=api, provider=IdleProvider(), os_port=FakeOs(), store_path=store_path)
    runtime.store.kv_set(OWNER_USER_KEY, OWNER_USER)
    runtime.store.kv_set(OWNER_CHAT_KEY, OWNER_CHAT)
    return runtime, api


def message(text: str, *, from_id: int = OWNER_USER, **extra) -> dict:
    return {"chat": {"id": OWNER_CHAT}, "from": {"id": from_id}, "text": text, **extra}


def callback(*, message_id: int, data: str) -> dict:
    return {"id": "cb1", "from": {"id": OWNER_USER}, "data": data,
            "message": {"message_id": message_id, "chat": {"id": OWNER_CHAT}}}


def open_card(runtime, *, summary: str = "Chrome ni och", ttl_s: float = 300.0,
              created_at: float | None = None):
    """A pending approval, shown the way the orchestrator shows one: through the reply path."""
    approval = runtime.store.approval_create(
        chat_id=OWNER_CHAT, tool="open_app", args={"name": "chrome"}, summary=summary,
        provenance=int(Provenance.OWNER), autonomy="ask_for_writes",
        generation=runtime.kill.generation, two_channel=False, ttl_s=ttl_s,
    )
    if created_at is not None:
        with runtime.store.transaction() as conn:
            conn.execute("UPDATE approvals SET created_at = ? WHERE id = ?", (created_at, approval.id))
    asyncio.run(runtime._reply(OWNER_CHAT, Reply(CARD + summary, runtime.approvals.buttons(approval),
                                                 approval.id)))
    return approval


def card_message_ids(api: HeldApi) -> list[int]:
    """Messages on the screen that carry a card's buttons, oldest first."""
    return [mid for mid, held in sorted(api.held.items())
            if held["buttons"] and held["text"].startswith(CARD)]


def sent_texts(api: HeldApi) -> list[str]:
    return [kwargs["text"] for name, kwargs in api.calls if name == "send_message"]


def record_owner_turns(runtime) -> list:
    calls: list = []

    async def handle_owner(chat_id, text, *, provenance=Provenance.OWNER, note=""):
        calls.append((chat_id, text, provenance, note))
        return Reply("")

    runtime.orchestrator.handle_owner = handle_owner
    return calls


def record_runs(runtime) -> list:
    ran: list[str] = []

    async def run_approved(approval_id, nonce, actor_id, owner_id):
        ran.append(approval_id)
        return Reply("✅ done")

    runtime.orchestrator.run_approved = run_approved
    return ran


# ------------------------------------------------------------- the answer words

@pytest.mark.parametrize("text", ["ha", "Ha", "ha!", "OK", "yes", "да", "Да.", "ҳа", "da", "DA"])
def test_a_yes_word_is_an_answer(text):
    assert approval_answer(text) == ANSWER_YES


@pytest.mark.parametrize("text", ["yo'q", "Yo’q", "yoq", "no", "нет", "йўқ", "net", "NO!"])
def test_a_no_word_is_an_answer(text):
    assert approval_answer(text) == ANSWER_NO


@pytest.mark.parametrize("text", [
    "shunday", "hayot", "okean", "not", "notebook", "netflix", "nothing",
    "ha ok", "no, change it", "da, lekin boshqacha", "ha ha", "", "   ", "✅",
])
def test_a_word_inside_a_longer_text_is_not_an_answer(text):
    assert approval_answer(text) is None


# ------------------------------------------------------- answers under a card

def test_a_typed_yes_changes_no_approval_row_and_runs_nothing(store_path):
    runtime, api = make_runtime(store_path)
    turns = record_owner_turns(runtime)
    runs = record_runs(runtime)
    card = open_card(runtime)
    before = runtime.store.approval_get(card.id)

    asyncio.run(runtime._on_message(message("ha")))

    assert runtime.store.approval_get(card.id) == before
    assert runs == []
    assert turns == []
    assert YES_REPLY in sent_texts(api)


def test_a_typed_no_declines_exactly_that_approval_and_strips_its_buttons(store_path):
    runtime, api = make_runtime(store_path)
    turns = record_owner_turns(runtime)
    card = open_card(runtime)
    (card_message,) = card_message_ids(api)

    asyncio.run(runtime._on_message(message("yo'q")))

    assert runtime.store.approval_get(card.id).status == DENIED
    assert api.held[card_message]["buttons"] == []
    assert sent_texts(api)[-1] == DECLINED
    assert turns == []


def test_with_two_open_cards_a_typed_no_declines_only_the_newer_one(store_path):
    runtime, api = make_runtime(store_path)
    older = open_card(runtime, summary="Chrome ni och", created_at=1_000.0)
    newer = open_card(runtime, summary="Telegram ni och", created_at=2_000.0)

    asyncio.run(runtime._on_message(message("no")))

    assert runtime.store.approval_get(older.id).status == PENDING
    assert runtime.store.approval_get(newer.id).status == DENIED


def test_other_text_starts_a_normal_turn_and_the_card_stays_pending(store_path):
    runtime, api = make_runtime(store_path)
    turns = record_owner_turns(runtime)
    card = open_card(runtime)
    (card_message,) = card_message_ids(api)

    asyncio.run(runtime._on_message(message("Telegram ni och")))

    assert turns == [(OWNER_CHAT, "Telegram ni och", Provenance.OWNER, "")]
    assert runtime.store.approval_get(card.id).status == PENDING
    assert api.held[card_message]["buttons"] != []


def test_a_typed_yes_re_sends_the_card_with_the_same_id_and_nonce_and_it_still_works(store_path):
    runtime, api = make_runtime(store_path)
    runs = record_runs(runtime)
    card = open_card(runtime)

    asyncio.run(runtime._on_message(message("ha")))

    assert len(runtime.store.approvals_pending(OWNER_CHAT)) == 1, "re-sending adds no approval row"
    first, resent = card_message_ids(api)
    assert [b["callback_data"] for b in api.held[resent]["buttons"][0]] == [
        f"ap:{card.id}:{card.nonce}:y", f"ap:{card.id}:{card.nonce}:n",
    ]
    assert api.held[first]["buttons"] != [], "the first card keeps its live buttons"

    asyncio.run(runtime._on_callback(callback(message_id=resent, data=f"ap:{card.id}:{card.nonce}:y")))

    assert runs == [card.id]


def test_an_expired_card_answered_with_yes_gets_the_expired_reply_and_runs_nothing(store_path):
    runtime, api = make_runtime(store_path)
    runs = record_runs(runtime)
    card = open_card(runtime, ttl_s=-1.0)
    (card_message,) = card_message_ids(api)

    asyncio.run(runtime._on_message(message("ha")))

    assert sent_texts(api)[-1] == EXPIRED_REPLY
    assert runs == []
    assert runtime.store.approval_get(card.id).status != APPROVED
    assert api.held[card_message]["buttons"] == [], "an expired card loses its buttons"
    assert card_message_ids(api) == []


@pytest.mark.parametrize("text", ["ha", "yo'q"])
def test_an_expired_card_answered_with_either_word_is_not_approved_or_declined(store_path, text):
    runtime, api = make_runtime(store_path)
    runs = record_runs(runtime)
    card = open_card(runtime, ttl_s=-1.0)

    asyncio.run(runtime._on_message(message(text)))

    assert sent_texts(api)[-1] == EXPIRED_REPLY
    assert runs == []
    assert runtime.store.approval_get(card.id).status not in (APPROVED, DENIED)


def test_an_open_question_keeps_its_buttons_when_a_typed_yes_answers_a_card(store_path):
    runtime, api = make_runtime(store_path)
    turns = record_owner_turns(runtime)
    runtime.outbox.ask(OWNER_CHAT, "Qaysi?", ["Ali", "Vali"])   # message 101
    open_card(runtime)                                           # message 102

    asyncio.run(runtime._on_message(message("ha")))

    assert turns == [], "the yes is answered by the card, not sent to the model"
    assert ("edit_reply_markup", {"chat_id": OWNER_CHAT, "message_id": 101}) not in api.calls


def test_a_card_that_was_never_sent_through_the_reply_path_is_not_stripped(store_path):
    """A card the owner never saw has no message to edit; a no still declines it and nothing else breaks."""
    runtime, api = make_runtime(store_path)
    card = runtime.store.approval_create(
        chat_id=OWNER_CHAT, tool="open_app", args={}, summary="x", provenance=int(Provenance.OWNER),
        autonomy="ask_for_writes", generation=runtime.kill.generation, two_channel=False, ttl_s=300,
    )

    asyncio.run(runtime._on_message(message("yo'q")))

    assert runtime.store.approval_get(card.id).status == DENIED
    assert not any(name == "edit_reply_markup" for name, _ in api.calls)
