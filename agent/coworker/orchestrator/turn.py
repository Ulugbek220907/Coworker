"""The turn loop: one owner message in, one reply out.

A turn is a bounded conversation with the model. Each model step either answers
or asks for tools; every tool call goes through the dispatcher, which applies
policy. A turn ends when the model answers, when a tool asks the owner to
approve something (the card is the reply), when the owner stops it, or when a
limit is reached. Limits are part of the design: a model that loops must not
keep the laptop busy.

Owner turns run one at a time. The pairing admits one owner chat, so one lock
keeps two of the owner's messages or taps from reading the same history and
interleaving their rows. Scheduled runs do not take the lock: they must not
hold the owner's chat for up to ten minutes.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..core.types import Autonomy, CancelToken, Decision, Provenance, Tier, ToolResult
from ..llm_base import NO_ANSWER, Msg, ProviderError
from ..scheduler import JobFailed
from ..store.approval_rows import PENDING
from ..tools.dispatch import Dispatcher, TurnState
from ..tools.registry import FAMILIES, Registry, Services
from . import prompt

log = logging.getLogger("orchestrator")

MAX_STEPS_INTERACTIVE = 9
MAX_STEPS_SCHEDULED = 20
TIME_INTERACTIVE = 180.0
TIME_SCHEDULED = 600.0
HISTORY_TURNS = 14
MAX_REPLY_CHARS = 1500
MODEL_MAX_TOKENS = 900
# The approval card's first line. It is the bot's message, never the model's words.
CARD_HEADER = "⚠️ Tasdiq kerak:"
# What an unattended run reports when it ended on a question: the question was sent, no answer yet.
ASKED_UNATTENDED = "Egasiga savol yuborildi; javobi kutilmoqda."

_AUTONOMY_RANK = {
    Autonomy.ASK_ALWAYS: 3,
    Autonomy.ASK_FOR_WRITES: 2,
    Autonomy.AUTONOMOUS_READONLY: 1,
    Autonomy.PANIC: 0,
}


def cap_autonomy(level: Autonomy, cap: Autonomy) -> Autonomy:
    """The stricter of two autonomy levels. Unattended runs never exceed AR."""
    return level if _AUTONOMY_RANK[level] <= _AUTONOMY_RANK[cap] else cap


@dataclass
class Reply:
    text: str
    buttons: list = field(default_factory=list)
    approval_id: Optional[str] = None
    failed: str = ""   # why a turn did not complete, in owner-facing words; empty when it did
    # Set by a tapped approval that ran: the note that lets the same request go on, and where.
    follow_up: Optional[str] = None
    chat_id: Optional[int] = None


class Orchestrator:
    def __init__(self, *, registry: Registry, dispatcher: Dispatcher, svc: Services,
                 config: Any, name: str = "Coworker") -> None:
        self.registry = registry
        self.dispatcher = dispatcher
        self.svc = svc
        self.config = config
        self.name = name
        self._owner_turns = asyncio.Lock()

    # ------------------------------------------------------------ configuration

    def grants(self) -> frozenset:
        disabled = set(self.config.get("disabled_families", []) or [])
        return frozenset(FAMILIES - disabled)

    def autonomy(self) -> Autonomy:
        if self.svc.kill is not None and self.svc.kill.is_panic():
            return Autonomy.PANIC
        try:
            return Autonomy(self.config.get("autonomy", Autonomy.ASK_FOR_WRITES.value))
        except ValueError:
            log.warning("unknown autonomy in config; using ask_for_writes")
            return Autonomy.ASK_FOR_WRITES

    def _generation(self) -> int:
        return self.svc.kill.generation if self.svc.kill is not None else 0

    def _provider(self):
        return self.svc.llm

    def _tool_schemas(self, grants: frozenset) -> list[dict]:
        if self._provider().dialect == "anthropic":
            return self.registry.anthropic_tools(grants)
        return self.registry.openai_tools(grants)

    # ------------------------------------------------------------ owner turns

    async def handle_owner(self, chat_id: int, text: str, *, provenance: Provenance = Provenance.OWNER,
                           note: str = "") -> Reply:
        """One owner turn.

        Text that is not the owner's own typing (a forwarded message, or an option
        the model offered) arrives with its real provenance. It is kept as content,
        so the origin, taint and relax rules treat it as they treat a document, and
        the model reads it framed by ``note``.
        """
        async with self._owner_turns:
            store = self.svc.store
            owner = provenance == Provenance.OWNER
            model_text = text if owner else prompt.framed(note, text)
            store.turn_add(chat_id, "user", model_text)
            turn = TurnState.new(
                chat_id, text if owner else "", actor="owner", autonomy=self.autonomy(), grants=self.grants(),
                generation=self._generation(),
                delivered={d["path"] for d in store.delivered_recent(chat_id, limit=25)},
            )
            if not owner:
                turn.add_content(text)
            return await self._run(turn, MAX_STEPS_INTERACTIVE, TIME_INTERACTIVE,
                                   chat_id=chat_id, model_text=model_text)

    async def run_approved(self, approval_id: str, nonce: str, actor_id: int, owner_id: int | None) -> Reply:
        """Execute a tapped approval, then let the same request go on from where it stopped.

        A request usually has several steps (open the project, then write into it). The tap
        runs one of them; the note sent to the model next says what ran and what it gave, so the
        remaining steps run in the same conversation instead of being dropped.
        """
        first = await self._run_tap(approval_id, nonce, actor_id, owner_id)
        if first.follow_up is None or first.chat_id is None:
            return first
        follow = await self.handle_owner(first.chat_id, first.follow_up)
        text = "\n\n".join(t for t in (first.text, follow.text) if t)
        return Reply(text, follow.buttons, follow.approval_id, follow.failed)

    async def _run_tap(self, approval_id: str, nonce: str, actor_id: int, owner_id: int | None) -> Reply:
        """Run the tapped action once, under the owner-turn lock."""
        async with self._owner_turns:
            approval = self.svc.approvals.consume(approval_id, nonce, actor_id, owner_id)
            if approval is None:
                return Reply("⚠️ Bu tasdiq eskirgan yoki allaqachon bajarilgan. Qaytadan so'rang.")
            kill = self.svc.kill
            if kill is not None and not kill.is_current(approval.generation):
                return Reply("⏹ To'xtatildi. Bu amal bekor qilindi.")
            turn = TurnState.new(
                approval.chat_id, "", actor="owner", autonomy=self.autonomy(), grants=self.grants(),
                generation=self._generation(),
                delivered={d["path"] for d in self.svc.store.delivered_recent(approval.chat_id, limit=25)},
            )
            # The run is registered with the kill switch, so /stop and /panic reach it
            # the same way they reach a turn: a running step is cancelled, and a call
            # still queued for a slot is dropped.
            if kill is not None:
                kill.register(turn.cancel)
            try:
                result = await self.dispatcher.execute_approved(approval, turn)
            finally:
                if kill is not None:
                    kill.unregister(turn.cancel)
            text = result_text(result, approval.summary)
            self.svc.store.turn_add(approval.chat_id, "assistant", text)
            follow = _continuation(result, approval.summary) if result.ok else None
            return Reply(text, follow_up=follow, chat_id=approval.chat_id)

    def awaiting_local(self, approval_id: str, nonce: str) -> bool:
        """True when a two-channel approval has the owner's tap but still lacks the laptop's OK.

        The runtime keeps the buttons in that case, so the owner can tap again once
        the laptop side is done instead of losing the action.
        """
        row = self.svc.store.approval_get(approval_id)
        return (
            row is not None and row.status == PENDING and row.two_channel and not row.local_ok
            and hmac.compare_digest(row.nonce.encode("utf-8"), nonce.encode("utf-8"))
            and row.expires_at > time.time()
        )

    def decline(self, approval_id: str, nonce: str, actor_id: int, owner_id: int | None) -> Reply:
        if not self.svc.approvals.decline(approval_id, nonce, actor_id, owner_id):
            return Reply("⚠️ Bu tasdiq eskirgan.")
        return Reply("❌ Bekor qilindi. Hech narsa o'zgarmadi.")

    # ---------------------------------------------------------- scheduled runs

    async def run_scheduled(self, instruction: str, chat_id: int) -> str:
        """Run a stored job unattended. Autonomy is capped at AR, always.

        A run that did not complete raises JobFailed, so the scheduler counts it as a
        failure. A run that never completes must not look like a success.
        """
        autonomy = cap_autonomy(self.autonomy(), Autonomy.AUTONOMOUS_READONLY)
        turn = TurnState.new(
            chat_id, instruction, actor="scheduler", autonomy=autonomy, grants=self.grants(),
            generation=self._generation(), cancel=CancelToken(),
        )
        reply = await self._run(turn, MAX_STEPS_SCHEDULED, TIME_SCHEDULED, chat_id=None, model_text=instruction)
        if reply.failed:
            raise JobFailed(reply.failed)
        # An empty result would be reported to the owner as a finished job. A run that ended
        # on a question has not finished, so it says what did happen.
        return reply.text or ASKED_UNATTENDED

    # --------------------------------------------------------------- the loop

    async def _run(self, turn: TurnState, max_steps: int, limit: float, *,
                   chat_id: Optional[int], model_text: str) -> Reply:
        kill = self.svc.kill
        if kill is not None:
            kill.register(turn.cancel)
        failed = ""
        try:
            text, failed = await asyncio.wait_for(self._loop(turn, max_steps, model_text), timeout=limit)
        except asyncio.TimeoutError:
            text, failed = "⏱ Vaqt tugadi. Ishni kichikroq qismlarga bo'lib so'rang.", "vaqt tugadi"
        except Exception:
            log.exception("turn %s crashed", turn.turn_id)
            text, failed = "⚠️ Ichki xato. Batafsil logda.", "ichki xato"
        finally:
            if kill is not None:
                kill.unregister(turn.cancel)

        if turn.pending is not None:
            # The card is not recorded in the history. Stored as assistant text, a later turn
            # would read a confirmation that nobody wrote, and could claim the action happened.
            card = f"{CARD_HEADER}\n{turn.pending.summary}"
            return Reply(card, self.svc.approvals.buttons(turn.pending), str(turn.pending.id))

        text = trim(text)
        if chat_id is not None:
            # A question was sent by the ask tool, not by this text. Its record keeps the
            # history from ending on an unanswered owner row, and gives the tap its context.
            if turn.asked:
                self.svc.store.turn_add(chat_id, "assistant", turn.asked)
            if text:
                self.svc.store.turn_add(chat_id, "assistant", text)
        return Reply(text, failed=failed)

    async def _loop(self, turn: TurnState, max_steps: int, model_text: str) -> tuple[str, str]:
        """The model loop. Returns the reply text and, when the turn did not complete, why."""
        provider = self._provider()
        # A scheduled run sees only its own instruction. The owner's history would let
        # it answer an old message of the owner's, which the job was not asked to do.
        messages = self._history(turn.chat_id) if turn.actor == "owner" and turn.chat_id is not None else []
        if not messages or messages[-1].role != "user":
            messages.append(Msg(role="user", content=model_text))
        system = self._system(turn)
        schemas = self._tool_schemas(turn.grants)

        for _ in range(max_steps):
            if turn.cancel.cancelled:
                return "⏹ To'xtatildi.", "to'xtatildi"

            admit = self.svc.budget.admit("llm_round", "llm", Tier.READ, turn.actor, turn.chat_id)
            if admit.decision == Decision.DENY:
                return "⛔ Bugungi AI limiti tugadi. Ertaga davom etamiz.", "AI limiti tugadi"

            try:
                out = await provider.chat(system, messages, schemas, max_tokens=MODEL_MAX_TOKENS)
            except ProviderError as exc:
                log.warning("provider failed: %s", exc)
                return f"⚠️ AI bilan aloqa yo'q.\n{exc}", "AI bilan aloqa yo'q"

            if not out.tool_calls:
                text = (out.text or "").strip()
                # An empty answer is not a finished task. Reporting it as done would tell
                # the owner that work happened which never ran.
                return (text, "") if text else (NO_ANSWER, "javob olinmadi")

            messages.append(Msg(role="assistant", content=out.text or None, tool_calls=out.tool_calls))
            for call in out.tool_calls:
                result = await self.dispatcher.invoke(call.name, call.args, turn)
                if call.name == "ask" and result.ok:
                    turn.asked = _asked_text(call.args)
                messages.append(Msg(
                    role="tool", tool_call_id=call.id, name=call.name,
                    content=prompt.wrap_result(result),
                ))
                if turn.pending is not None or turn.end_turn:
                    break
            if turn.pending is not None:
                return "", ""
            if turn.end_turn:
                # The ask tool has already sent the question. The model's text beside that call
                # was written before the tool ran, so it is not reported to the owner as a result.
                return "", ""

        return "Qadamlar tugadi. Savolni qisqaroq qilib qayta yozing.", "qadamlar tugadi"

    # ----------------------------------------------------------------- context

    def _system(self, turn: TurnState) -> str:
        store = self.svc.store
        facts: list[str] = []
        summary = ""
        delivered: list[str] = []
        if turn.chat_id is not None:
            facts = [f["text"] for f in store.facts_list(turn.chat_id, limit=30) if not f.get("untrusted")]
            summary = store.summary_get(turn.chat_id) or ""
            delivered = [d["path"] for d in store.delivered_recent(turn.chat_id, limit=8)]
        return prompt.system_prompt(
            name=self.name, grants=turn.grants, autonomy=turn.autonomy,
            facts=facts, summary=summary, delivered=delivered,
        )

    def _history(self, chat_id: int) -> list[Msg]:
        rows = self.svc.store.turns_recent(chat_id, limit=HISTORY_TURNS)
        # Cards that earlier versions stored as assistant text are skipped too, so they stop
        # being replayed to the model once this version runs.
        msgs = [Msg(role=r["role"], content=r["content"]) for r in rows
                if r.get("content") and not _is_card(r)]
        while msgs and msgs[0].role != "user":
            msgs.pop(0)
        return msgs


def _continuation(result: ToolResult, summary: str) -> str:
    """The note that lets a request go on after one of its steps was approved and ran."""
    message = result.data.get("message") if isinstance(result.data, dict) else None
    done = message or summary
    return (
        f"[Tasdiqlangan amal bajarildi: {summary}. Natija: {done}] "
        "Oldingi so'rovda yana qadam qolganmi? Bo'lsa, davom et. Qolmagan bo'lsa, "
        "bitta qisqa xulosa yoz; natijani faqat yuqoridagi natija bo'yicha ayt."
    )


def result_text(result: ToolResult, summary: str = "") -> str:
    """What the owner reads after a tapped action: what the tool reported, never a bare "done".

    The tool's own message says what was verified. Without one, the card's summary says
    what was asked for, so the owner is not told that something happened that nobody checked.
    """
    if result.ok:
        message = result.data.get("message") if isinstance(result.data, dict) else None
        if message:
            return f"✅ {message}"
        return f"✅ Bajarildi: {summary}" if summary else "✅ Bajarildi."
    return f"❌ {result.error or result.code or 'Bajarilmadi'}"


def trim(text: str, limit: int = MAX_REPLY_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    idx = cut.rfind("\n")
    if idx > limit * 0.6:
        cut = cut[:idx]
    return cut.rstrip() + "…"


def _asked_text(args: dict) -> str:
    question = str(args.get("question", "")).strip()
    options = [str(o) for o in args.get("options") or []]
    return f"{question}\nVariantlar: {' / '.join(options)}"


def _is_card(row: dict) -> bool:
    return row.get("role") == "assistant" and str(row.get("content", "")).startswith(CARD_HEADER)
