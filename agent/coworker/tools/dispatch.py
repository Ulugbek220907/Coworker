"""The only path from a model request to a side effect.

A tool never runs because the model asked for it directly. The dispatcher:

  1. finds the spec and checks it is visible under the turn's grants;
  2. validates the arguments against the declared schema;
  3. asks the policy kernel for a verdict on a snapshot of the turn;
  4. DENY: audits the refusal and returns it to the model;
     CONFIRM: freezes the arguments as an approval card and ends the turn;
     ALLOW: charges the budget, audits the intent, runs the handler under the
     governor and audits the outcome;
  5. folds the result into the turn: untrusted output raises the provenance to
     CONTENT, search results make their paths sendable.

Approved actions come back through ``execute_approved``. The kernel is consulted
again only to refuse: the owner has already seen the exact arguments. The turn
that runs them gets the provenance and the owner's words of the turn that
proposed them, so the handlers' own origin checks still see the frozen state.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..core.types import (
    Autonomy,
    CallContext,
    Cancelled,
    CancelToken,
    Decision,
    Provenance,
    Tier,
    ToolResult,
    Verdict,
    normalize_text,
)
from ..policy import prohibited
from ..policy.kernel import PolicyKernel
from .registry import Registry, Services, ToolCall, ToolSpec, validate_args

log = logging.getLogger("dispatch")

CONTENT_CAP = 200_000       # normalised characters of untrusted text kept per turn
# Tools that put text into an app or a page. Their text is scanned for card numbers
# together with the tail of what this turn typed before, so a split card is caught.
TYPED_TEXT_TOOLS = frozenset({"key_type", "clipboard_set", "ui_set_text", "web_type", "control_app_send"})
TYPED_TAIL_CHARS = 64
# Families whose successful READ calls return data from this machine.
LOCAL_READ_FAMILIES = frozenset({"files", "office", "desktop", "notes"})
ARGS_SUMMARY_CAP = 512      # audit rows keep at most this much of the arguments
# Tools whose text argument is typed into an app or copied to the clipboard. The audit
# keeps its length and hash only, never the value, so a typed password stays out of the log.
TEXT_ARGUMENTS = {
    "key_type": "text",
    "ui_set_text": "text",
    "web_type": "text",
    "clipboard_set": "text",
    "control_app_send": "text",
}
# Governor refusals raised before the handler starts: nothing ran, so "not run" is true.
NOT_RUN_CODES = ("throttled", "low_memory", "locked_desktop", "paused", "timeout")
UNKNOWN_OUTCOME = "the action started but did not report back; it may have run. Check the current state before retrying."
FROZEN_KEY = "approval_frozen:{}"   # kv key of the owner's words of the turn that proposed an approval


def path_key(value: str) -> str:
    return os.path.normcase(os.path.normpath(value))


def audit_view(name: str, args: dict) -> dict:
    """The arguments as the audit may keep them: typed or copied text becomes length and hash."""
    field_name = TEXT_ARGUMENTS.get(name)
    value = args.get(field_name) if field_name else None
    if not isinstance(value, str):
        return args
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return {**args, field_name: f"[text: {len(value)} chars, sha256:{digest}]"}


def summarize_args(args: dict) -> str:
    """Audit-safe view of arguments: secrets redacted, length capped."""
    try:
        text = json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(args)
    return prohibited.redact(text)[:ARGS_SUMMARY_CAP]


def text_leaves(value: Any) -> list[str]:
    """The text inside a result, one string per leaf, with no JSON escaping.

    Origin checks compare normalised text. JSON escaping doubles backslashes and
    quotes, so a Windows path read from a page would never match the same path
    in the model's own command. Keys are labels, not content, and are left out.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in text_leaves(item)]
    if isinstance(value, (list, tuple)):
        return [leaf for item in value for leaf in text_leaves(item)]
    return [str(value)]


@dataclass
class TurnState:
    """Mutable state of one turn. Never shared between chats."""

    turn_id: str
    chat_id: Optional[int]
    actor: str                       # "owner" or "scheduler"
    autonomy: Autonomy
    grants: frozenset
    generation: int
    owner_text: str
    cancel: CancelToken
    provenance: Provenance = Provenance.OWNER
    local_read: bool = False
    content_parts: list = field(default_factory=list)
    content_chars: int = 0
    surfaced: set = field(default_factory=set)
    delivered: set = field(default_factory=set)
    sendable: list = field(default_factory=list)
    end_turn: bool = False
    pending: Any = None              # the Approval that ended the turn, if any
    typed_tail: str = ""             # the last characters this turn typed, for split card numbers
    asked: str = ""                  # the question the owner was asked this turn, kept in history
    tool_calls: int = 0

    @classmethod
    def new(cls, chat_id: Optional[int], text: str, *, actor: str, autonomy: Autonomy,
            grants: frozenset, generation: int, delivered: set | None = None,
            cancel: CancelToken | None = None) -> "TurnState":
        return cls(
            turn_id=uuid.uuid4().hex[:12],
            chat_id=chat_id,
            actor=actor,
            autonomy=autonomy,
            grants=grants,
            generation=generation,
            owner_text=text,
            cancel=cancel or CancelToken(),
            delivered={path_key(p) for p in (delivered or set())},
        )

    def typed_card_refusal(self, text: str) -> Optional[str]:
        """The refusal code when typed text completes a card number begun by an earlier call."""
        joined = self.typed_tail + " " + text
        self.typed_tail = joined[-TYPED_TAIL_CHARS:]
        return prohibited.scan_text(joined)

    def snapshot(self) -> CallContext:
        return CallContext(
            turn_id=self.turn_id,
            actor=self.actor,
            chat_id=self.chat_id,
            autonomy=self.autonomy,
            grants=self.grants,
            generation=self.generation,
            provenance=self.provenance,
            owner_norm=normalize_text(self.owner_text),
            content_norm=normalize_text("\n".join(self.content_parts)),
            surfaced=frozenset(self.surfaced),
            delivered=frozenset(self.delivered),
            local_read=self.local_read,
            cancel=self.cancel,
        )

    def add_content(self, text: str) -> None:
        """Keep untrusted text for the origin checks, up to CONTENT_CAP characters per turn."""
        self.provenance = max(self.provenance, Provenance.CONTENT)
        if self.content_chars < CONTENT_CAP:
            piece = text[: CONTENT_CAP - self.content_chars]
            self.content_parts.append(piece)
            self.content_chars += len(piece)

    def absorb(self, spec: ToolSpec, result: ToolResult) -> None:
        """Fold one tool result into the turn's taint and path state."""
        self.tool_calls += 1
        if result.ok and spec.tier == Tier.READ and spec.family in LOCAL_READ_FAMILIES and not spec.egress:
            self.local_read = True
        if result.untrusted or spec.untrusted:
            self.add_content("\n".join(text_leaves(result.to_dict())))
        for path in result.surfaced:
            self.surfaced.add(path_key(path))
        if result.surfaced:
            self.provenance = max(self.provenance, Provenance.METADATA)
        for path in result.sendable:
            self.surfaced.add(path_key(path))
            self.sendable.append(path)
        if result.ok and result.data.get("end_turn"):
            self.end_turn = True


class Dispatcher:
    def __init__(self, registry: Registry, kernel: PolicyKernel, svc: Services, *, provider: str = "") -> None:
        self.registry = registry
        self.kernel = kernel
        self.svc = svc
        self.provider = provider

    # ------------------------------------------------------------- model calls

    async def invoke(self, name: str, args: Any, turn: TurnState) -> ToolResult:
        spec = self.registry.get(name)
        visible = self.registry.visible_names(turn.grants)
        if spec is None or name not in visible:
            code = "unknown_tool" if spec is None else "not_granted"
            return self._refuse(turn, name, args, Verdict.deny(code, f"tool {name!r} is not available"), audit=True)

        err = validate_args(spec.parameters, args)
        if err:
            return ToolResult.fail("arg_invalid", err)

        typed = args.get("text") if name in TYPED_TEXT_TOOLS and isinstance(args.get("text"), str) else None
        if typed is not None:
            code = turn.typed_card_refusal(typed)
            if code:
                return self._refuse(turn, name, args,
                                    Verdict.deny(code, "the typed text completes a card number begun earlier"),
                                    tier=spec.tier, spec=spec)

        ctx = turn.snapshot()
        verdict = self.kernel.evaluate(spec, args, ctx, self.svc)
        if verdict.decision == Decision.DENY:
            return self._refuse(turn, name, args, verdict, tier=spec.tier, spec=spec)
        if verdict.decision == Decision.CONFIRM:
            return self._propose(turn, spec, args, verdict, ctx)
        return await self._execute(spec, args, turn, ctx, verdict)

    # --------------------------------------------------------- approved calls

    async def execute_approved(self, approval: Any, turn: TurnState) -> ToolResult:
        """Run an action the owner tapped. The frozen arguments are used as-is.

        The owner already saw the exact arguments, so the paths in the card count
        as surfaced for this run. The kernel is consulted again only to refuse:
        a CONFIRM at this point is the approval itself, not a new question.
        """
        self._thaw(approval, turn)
        try:
            spec = self.registry.get(approval.tool)
            if spec is None:
                return ToolResult.fail("unknown_tool", "action no longer exists")
            args = dict(approval.args)
            for name in spec.requires_surfaced:
                value = args.get(name)
                if isinstance(value, str) and value:
                    turn.surfaced.add(path_key(value))
            ctx = turn.snapshot()
            verdict = self.kernel.evaluate(spec, args, ctx, self.svc)
            if verdict.decision == Decision.DENY:
                return self._refuse(turn, spec.name, args, verdict, tier=spec.tier, spec=spec)
            return await self._execute(spec, args, turn, ctx, verdict)
        finally:
            self._forget(approval.id)

    # ------------------------------------------------------------- internals

    def _refuse(self, turn: TurnState, name: str, args: Any, verdict: Verdict,
                tier: Optional[Tier] = None, spec: Optional[ToolSpec] = None, audit: bool = True) -> ToolResult:
        if audit:
            intent = self._intent(turn, name, args, tier, Decision.DENY, verdict.code, spec)
            self._outcome(intent, False, verdict.code, verdict.reason)
        return ToolResult.fail(verdict.code, verdict.reason or verdict.code)

    def _propose(self, turn: TurnState, spec: ToolSpec, args: dict, verdict: Verdict, ctx: CallContext) -> ToolResult:
        intent = self._intent(turn, spec.name, args, spec.tier, Decision.CONFIRM, verdict.code, spec)
        approval = self.svc.approvals.propose(turn.chat_id, spec, args, verdict, ctx)
        self._freeze(approval.id, turn)
        self._outcome(intent, False, "awaiting_confirm", verdict.summary)
        turn.pending = approval
        turn.end_turn = True
        return ToolResult(
            ok=False,
            code="awaiting_confirm",
            error="the owner has been asked to approve this; wait for the answer",
            data={"approval_id": approval.id, "summary": verdict.summary},
        )

    async def _execute(self, spec: ToolSpec, args: dict, turn: TurnState, ctx: CallContext,
                       verdict: Verdict) -> ToolResult:
        # Approved calls are charged too. The card is not charged: the budget limits
        # what the agent does in a day, and an approved send or delete does it.
        budget = self.svc.budget.admit(spec.name, spec.family, spec.tier, turn.actor, turn.chat_id)
        if budget.decision == Decision.DENY:
            return self._refuse(turn, spec.name, args, budget, tier=spec.tier, spec=spec)

        intent = self._intent(turn, spec.name, args, spec.tier, Decision.ALLOW, verdict.code, spec)
        call = ToolCall(name=spec.name, args=args, ctx=ctx, svc=self.svc)
        # The governor cannot tell a job that never started from one that is running
        # when it gives up. This flag records the start, so a stuck action is reported
        # as unknown instead of as "not run".
        started = threading.Event()

        def run_handler() -> ToolResult:
            started.set()
            return spec.handler(call)

        try:
            result = await self.svc.governor.run(
                spec.gov_class, run_handler,
                timeout_s=spec.timeout_s, cancel=turn.cancel,
                # Only an owner turn may use the interactive exemptions; unattended work waits.
                interactive=turn.actor == "owner",
            )
        except Cancelled:
            result = _unknown() if started.is_set() else ToolResult.fail("cancelled", "stopped by the owner")
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code == "timeout" and started.is_set():
                result = _unknown()
            elif code in NOT_RUN_CODES:
                result = ToolResult.fail(code, f"not run: {code}")
            else:
                log.exception("tool %s failed", spec.name)
                result = ToolResult.fail("tool_error", "the action failed; details are in the log")
        if not isinstance(result, ToolResult):
            log.error("tool %s returned %r instead of ToolResult", spec.name, type(result))
            result = ToolResult.fail("tool_error", "malformed tool result")
        # A failure text from an untrusted tool can carry the same page or file text
        # as its success text, so the label goes on both.
        if spec.untrusted:
            result.untrusted = True

        self._outcome(intent, result.ok, result.code or ("ok" if result.ok else "failed"), result.error or "")
        turn.absorb(spec, result)
        return result

    def _freeze(self, approval_id: str, turn: TurnState) -> None:
        """Keep the owner's words of the proposing turn until the tap.

        The approval row holds the provenance; the words are kept beside it so the
        approved run can apply the same second-line origin check as a fresh turn.
        """
        store = self.svc.store
        if store is not None:
            store.kv_set(FROZEN_KEY.format(approval_id), {"owner_text": turn.owner_text})

    def _thaw(self, approval: Any, turn: TurnState) -> None:
        """Give an approved call the provenance and the owner's words it was proposed with."""
        turn.provenance = max(turn.provenance, Provenance(int(approval.provenance)))
        store = self.svc.store
        frozen = store.kv_get(FROZEN_KEY.format(approval.id)) if store is not None else None
        if isinstance(frozen, dict):
            turn.owner_text = str(frozen.get("owner_text", ""))

    def _forget(self, approval_id: str) -> None:
        store = self.svc.store
        if store is not None:
            store.kv_set(FROZEN_KEY.format(approval_id), None)

    def _intent(self, turn: TurnState, name: str, args: Any, tier: Optional[Tier],
                decision: Decision, code: str, spec: Optional[ToolSpec]) -> Optional[int]:
        store = self.svc.store
        if store is None:
            return None
        audited = audit_view(name, args) if isinstance(args, dict) else {"raw": str(args)}
        return store.audit_intent(
            turn.turn_id,
            turn.actor,
            name,
            tier.value if isinstance(tier, Tier) else "",
            decision.value,
            code,
            summarize_args(audited),
            self.provider,
        )

    def _outcome(self, intent: Optional[int], ok: bool, code: str, summary: str) -> None:
        store = self.svc.store
        if store is None or intent is None:
            return
        store.audit_outcome(intent, ok, code, prohibited.redact(summary or "")[:ARGS_SUMMARY_CAP])


def _unknown() -> ToolResult:
    return ToolResult.fail("outcome_unknown", UNKNOWN_OUTCOME)
