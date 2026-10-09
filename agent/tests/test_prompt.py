"""The system prompt's intent and honesty rules, and the orchestrator behaviour they rely on.

The provider is a script, not a model. Each step either answers a fixed text or,
given the messages so far, decides what to do next, as a model would after a
tool result. No network is used. The checks are that the prompt states the
rules, and that the orchestrator carries a tool result and the owner's words to
the model the way the prompt says it will.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from types import SimpleNamespace

from coworker.core.types import Autonomy, CallContext, Provenance, ToolResult
from coworker.governor.budget import Budget
from coworker.orchestrator import prompt
from coworker.orchestrator.turn import ASKED_UNATTENDED, CARD_HEADER, Orchestrator
from coworker.safety import ApprovalBroker, KillSwitch
from coworker.tools import telegram_out
from coworker.tools.registry import FAMILIES, Registry, Services, ToolCall, validate_args
from coworker.transport.outbox import Outbox
from fakes.telegram_fake import FakeBotApi, FakeBudget, FakeStore

NOW = datetime(2026, 10, 9, 10, 0)
CANCEL = prompt.CANCEL_OPTION


def system(grants: frozenset = FAMILIES) -> str:
    return prompt.system_prompt(
        name="Coworker", grants=grants, autonomy=Autonomy.ASK_FOR_WRITES,
        facts=[], summary="", delivered=[], now=NOW,
    )


def _block(text: str, heading: str) -> str:
    """The paragraph that starts at ``heading``. A blank line ends it."""
    start = text.index(heading)
    end = text.find("\n\n", start)
    return text[start:] if end == -1 else text[start:end]


# ------------------------------------------------------------ the prompt text

def test_the_intent_rules_are_in_the_prompt_even_with_no_family_granted():
    assert "NIYAT" in system(frozenset())
    assert "NIYAT" in system()


def test_the_prompt_names_the_four_intents():
    text = system()
    for intent in ("DASTUR", "BRAUZER", "SAYT", "BOSHQA"):
        assert intent in text


def test_the_decision_order_names_the_lookups_before_the_actions():
    text = system()
    assert "TARTIB" in text
    assert "lookup asbobini chaqir" in text
    for lookup in ("find_app", "installed_browsers", "default_browser"):
        assert lookup in text


def test_the_program_intent_covers_each_lookup_status():
    program = _block(system(), "DASTUR (lookup")
    for status in ("exact", "choose", "none", "refused"):
        assert f"status {status}" in program
    assert "open_app" in program
    assert "ochildi" in program                       # says what opened, after the ok result
    assert "bitta oddiy savol" in program             # none: one plain question
    assert "Boshqa nomlar bilan qayta qidirma" in program  # no retries under other names


def test_the_browser_intent_never_uses_web_open_and_asks_with_the_installed_browsers():
    browser = _block(system(), "BRAUZER (lookup")
    assert "web_open HECH QACHON chaqirilmaydi" in browser
    assert "installed_browsers" in browser and "default_browser" in browser
    assert "open_app" in browser
    assert "(standart)" in browser                    # the default is marked in its label
    assert CANCEL in browser                          # and the choice ends with cancel


def test_the_site_intent_uses_web_open_only_with_a_known_address_and_asks_in_plain_text():
    site = _block(system(), "SAYT (manzil")
    assert "web_open" in site
    assert "to'liq manzilni so'ra" in site
    assert "Tugmasiz" in site


def test_the_choice_rules_send_choices_only_through_the_ask_tool():
    choice = _block(system(), "TANLOV (ask)")
    assert "ask asbobi bilan" in choice
    assert "tugma yoki tasdiq kartasi matnini o'zing yozma" in choice
    assert f"«{CANCEL}» tanlansa" in choice


def test_the_error_codes_need_choice_and_not_found_have_their_rules():
    choice = _block(system(), "TANLOV (ask)")
    assert "need_choice" in choice and "xatodagi variantlar bilan ask" in choice
    assert "not_found" in choice and "bitta savol" in choice


def test_a_typed_yes_is_not_an_approval():
    assert "tasdiq faqat tasdiq tugmasi orqali" in system()


def test_the_honesty_rule_says_success_only_from_an_ok_result_of_this_turn():
    honesty = _block(system(), "HALOLLIK (majburiy)")
    assert "shu turnda asbob ok qaytargandagina" in honesty
    assert "ochildi" in honesty and "ochilmadi" in honesty
    assert "chaqirmasdan natija haqida gapirma" in honesty


def test_the_prompt_has_no_phrase_list_for_the_browser_or_the_program():
    text = system().lower()
    # Trigger phrases ("X ni och", "brauzerni och") and program names. The intent words
    # themselves (brauzer, dastur, ochish) are meanings, so they are not on this list.
    forbidden = [
        r"\w+ni och", r"\bbrauzerni och", r"\bdasturni och", r"\bopen (the )?(browser|app)",
        r"\bchrome\b", r"\bfirefox\b", r"\bedge\b", r"\bbrave\b", r"\bopera\b", r"\bnotepad\b",
        r"\bwriter\b", r"\boffice\b", r"\bgoogle\b", r"\bmicrosoft\b",
    ]
    for pattern in forbidden:
        assert re.search(pattern, text) is None, pattern


def test_the_cancel_option_is_one_text_for_the_prompt_and_the_ask_tool():
    assert prompt.CANCEL_OPTION == telegram_out.CANCEL_OPTION == "Bekor qilish"


def test_a_tool_error_keeps_its_code_in_the_text_the_model_reads():
    text = prompt.wrap_result(ToolResult.fail("need_choice", "variantlar: A, B"))
    assert '"code": "need_choice"' in text


# ------------------------------------------------------------ the ask tool

def _ask_spec():
    return next(s for s in telegram_out.SPECS if s.name == "ask")


def test_the_ask_schema_accepts_a_choice_that_ends_with_the_cancel_option():
    args = {"question": "Qaysi biri?", "options": ["Dastur A", "Dastur B", CANCEL]}
    assert validate_args(_ask_spec().parameters, args) is None


def test_the_ask_schema_refuses_seven_options_so_the_cancel_option_is_never_cut():
    args = {"question": "?", "options": [f"d{i}" for i in range(6)] + [CANCEL]}
    assert validate_args(_ask_spec().parameters, args) is not None


def _ask_call(args: dict, svc: Services) -> ToolCall:
    context = CallContext(
        turn_id="t1", actor="owner", chat_id=4242, autonomy=Autonomy.ASK_FOR_WRITES,
        grants=frozenset({"telegram"}), generation=0, provenance=Provenance.OWNER,
        owner_norm="", content_norm="", surfaced=frozenset(), delivered=frozenset(),
    )
    return ToolCall(name="ask", args=args, ctx=context, svc=svc)


def _ask_services() -> tuple[Services, FakeBotApi]:
    store = FakeStore()
    store.kv["owner_chat_id"] = 4242
    api = FakeBotApi()
    return Services(store=store, budget=FakeBudget(), outbox=Outbox(api, store)), api


def test_the_ask_tool_sends_the_cancel_option_last_as_the_final_button():
    svc, api = _ask_services()

    result = telegram_out._ask(_ask_call({"question": "Qaysi biri?", "options": ["Dastur A", "Dastur B", CANCEL]}, svc))

    assert result.ok is True and result.data["end_turn"] is True
    _, kwargs = api.calls[0]
    assert [row[0]["text"] for row in kwargs["buttons"]] == ["Dastur A", "Dastur B", CANCEL]


def test_the_ask_tool_refuses_more_than_six_options_before_anything_is_sent():
    svc, api = _ask_services()
    options = [f"d{i}" for i in range(6)] + [CANCEL]

    result = telegram_out._ask(_ask_call({"question": "?", "options": options}, svc))

    assert (result.ok, result.code) == (False, "arg_invalid")
    assert api.calls == []


# ----------------------------------------------- orchestrator, scripted model

def answer(text: str = "", calls=()) -> SimpleNamespace:
    return SimpleNamespace(text=text, tool_calls=list(calls))


def call(name: str, args: dict, call_id: str = "c1") -> SimpleNamespace:
    return SimpleNamespace(id=call_id, name=name, args=args)


def _saw_code(messages, code: str) -> bool:
    return any(m.role == "tool" and f'"code": "{code}"' in (m.content or "") for m in messages)


def asks_after(code: str, question: str, options: list[str]):
    """A scripted step: after a tool result with ``code``, ask the owner; otherwise say nothing new."""
    def step(messages):
        if _saw_code(messages, code):
            return answer("", [call("ask", {"question": question, "options": options}, "c2")])
        return answer("Tushunmadim.")
    return step


class ScriptedProvider:
    dialect = "openai"
    name = "scripted"

    def __init__(self, *steps) -> None:
        self._steps = list(steps)
        self.seen: list[list] = []

    async def chat(self, system, messages, tools, max_tokens=900):
        self.seen.append(list(messages))
        step = self._steps.pop(0) if self._steps else answer("")
        return step(messages) if callable(step) else step


class FakeTools:
    """Answers each tool with a fixed result and records the calls. ``pending`` ends the turn as a card."""

    def __init__(self, results: dict[str, ToolResult], pending=None) -> None:
        self.results = results
        self.pending = pending
        self.calls: list[tuple[str, dict]] = []

    async def invoke(self, name, args, turn):
        self.calls.append((name, dict(args)))
        if self.pending is not None and name in self.results and not self.results[name].ok:
            turn.pending = self.pending
            turn.end_turn = True
            return self.results[name]
        result = self.results[name]
        if result.ok and result.data.get("end_turn"):
            turn.end_turn = True
        return result

    async def execute_approved(self, approval, turn):
        return ToolResult(ok=True, data={"message": "done"})


ASKED = ToolResult(ok=True, data={"asked": True, "end_turn": True})


def make_orchestrator(store, provider, tools, kill=None):
    kill = kill or KillSwitch(store)
    svc = Services(store=store, budget=Budget(store), approvals=ApprovalBroker(store), llm=provider, kill=kill)
    return Orchestrator(registry=Registry(), dispatcher=tools, svc=svc, config={}), kill


def test_a_need_choice_error_makes_the_model_ask_with_the_options(store):
    tools = FakeTools({
        "open_app": ToolResult.fail("need_choice", "the name matches more than one installed application: Dastur A, Dastur B."),
        "ask": ASKED,
    })
    provider = ScriptedProvider(
        answer("", [call("open_app", {"name": "Dastur"})]),
        asks_after("need_choice", "Qaysi biri?", ["Dastur A", "Dastur B", CANCEL]),
    )
    orchestrator, _ = make_orchestrator(store, provider, tools)

    reply = asyncio.run(orchestrator.handle_owner(42, "open dastur"))

    assert [name for name, _ in tools.calls] == ["open_app", "ask"]
    assert tools.calls[1][1]["options"] == ["Dastur A", "Dastur B", CANCEL]
    assert reply.text == "" and reply.failed == ""
    last = store.turns_recent(42)[-1]
    assert last["role"] == "assistant"
    assert f"Qaysi biri?\nVariantlar: Dastur A / Dastur B / {CANCEL}" == last["content"]


def test_a_not_found_error_makes_the_model_ask_one_question_for_the_exact_name(store):
    tools = FakeTools({
        "open_app": ToolResult.fail("not_found", "no installed application has this name."),
        "ask": ASKED,
    })
    provider = ScriptedProvider(
        answer("", [call("open_app", {"name": "zzq"})]),
        asks_after("not_found", "Dastur nomini aniq ayting?", [CANCEL]),
    )
    orchestrator, _ = make_orchestrator(store, provider, tools)

    asyncio.run(orchestrator.handle_owner(42, "open zzq"))

    assert [name for name, _ in tools.calls] == ["open_app", "ask"]


def test_an_approval_card_is_not_replayed_to_the_model_as_its_own_words(store):
    kill = KillSwitch(store)
    approval = store.approval_create(
        chat_id=42, tool="open_app", args={"name": "Dastur"}, summary="Dasturni ochish: Dastur",
        provenance=int(Provenance.OWNER), autonomy="ask_for_writes", generation=kill.generation,
        two_channel=False, ttl_s=300,
    )
    tools = FakeTools({"open_app": ToolResult.fail("awaiting_confirm", "wait for the answer")}, pending=approval)
    provider = ScriptedProvider(
        answer("", [call("open_app", {"name": "Dastur"})]),
        answer("Tushunarli."),
    )
    orchestrator, _ = make_orchestrator(store, provider, tools, kill)

    reply = asyncio.run(orchestrator.handle_owner(42, "open dastur"))

    assert reply.text.startswith(CARD_HEADER) and reply.approval_id == str(approval.id)
    assert [row["role"] for row in store.turns_recent(42)] == ["user"]
    asyncio.run(orchestrator.handle_owner(42, "Ha"))
    assert not any(CARD_HEADER in (m.content or "") for m in provider.seen[1])


def test_a_card_stored_by_an_earlier_version_is_not_replayed(store):
    store.turn_add(42, "user", "open dastur")
    store.turn_add(42, "assistant", f"{CARD_HEADER}\nDasturni ochish: Dastur")
    provider = ScriptedProvider(answer("Tushunarli."))
    orchestrator, _ = make_orchestrator(store, provider, FakeTools({}))

    asyncio.run(orchestrator.handle_owner(42, "Ha"))

    assert not any(CARD_HEADER in (m.content or "") for m in provider.seen[0])


def test_the_models_own_text_beside_an_ask_is_not_sent_as_a_result(store):
    tools = FakeTools({"ask": ASKED})
    provider = ScriptedProvider(
        answer("Chrome ochildi.", [call("ask", {"question": "Qaysi?", "options": ["A", CANCEL]})]),
    )
    orchestrator, _ = make_orchestrator(store, provider, tools)

    reply = asyncio.run(orchestrator.handle_owner(42, "open something"))

    assert reply.text == ""
    assert not any("ochildi" in r["content"] for r in store.turns_recent(42))


def test_a_scheduled_run_that_asked_does_not_report_itself_as_done(store):
    tools = FakeTools({"ask": ASKED})
    provider = ScriptedProvider(answer("", [call("ask", {"question": "Qaysi?", "options": ["A", CANCEL]})]))
    orchestrator, _ = make_orchestrator(store, provider, tools)

    text = asyncio.run(orchestrator.run_scheduled("send the report", 42))

    assert text == ASKED_UNATTENDED
