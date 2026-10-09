"""The policy kernel: the one place that decides whether a tool call may run.

Every call goes through ``PolicyKernel.evaluate``. The order below is fixed.
A DENY from any early step ends the evaluation; later steps only make a
decision stricter, except the relax hook, which may relax a single tool's
default inside the owner's own turn under ask_for_writes.

    1  unknown tool                     -> DENY unknown_tool
    2  panic                           -> DENY panic
    3  family not granted              -> DENY not_granted
    4  FINANCIAL / CREDENTIAL tier     -> DENY prohibited_tier
    5  path arguments                  -> DENY (path code)
    6  prohibited content in arguments -> DENY (prohibited_*)
    7  surfaced-only arguments         -> DENY not_surfaced
    8  content-only atoms (origin)     -> DENY origin_content
    9  tier default (matrix)           -> ALLOW / CONFIRM / DENY
   10  relax hook (owner, AW only)     -> ALLOW (relaxed)
   11  taint escalation (CONTENT or    -> CONFIRM, or DENY when unattended
       METADATA)
   11a egress after a local read       -> CONFIRM, or DENY when unattended
   12  tool argument hooks             -> ALLOW / CONFIRM / DENY
   13  strictest of 9, 11 and 12 wins; CONFIRM gets a summary

Budgets and rate limits are not part of the kernel; the dispatcher checks them
after a CONFIRM or ALLOW, because they depend on state the kernel must not read.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Optional

from ..core.types import Autonomy, CallContext, Decision, Provenance, Tier, Verdict, strictest
from . import matrix, origin, paths, prohibited

if TYPE_CHECKING:  # pragma: no cover
    from ..tools.registry import Services, ToolSpec

log = logging.getLogger("policy")

# Tiers where a value copied from untrusted content is refused (step 8).
ORIGIN_TIERS = (Tier.OUTBOUND, Tier.DESTRUCTIVE, Tier.SYSTEM_CHANGE)


def _key(value: str) -> str:
    return os.path.normcase(os.path.normpath(value))


def _without_owner_paths(args: dict, names: tuple, ctx: CallContext) -> dict:
    """The arguments with every path the owner's own search asked for removed, value by value."""
    out = dict(args)
    for name in names:
        value = args.get(name)
        if isinstance(value, str) and _key(value) in ctx.surfaced_owner:
            out[name] = ""
        elif isinstance(value, list):
            out[name] = [v for v in value if not (isinstance(v, str) and _key(v) in ctx.surfaced_owner)]
    return out


def _strings(value: Any) -> list[str]:
    """The non-empty strings in an argument: a single path, or each path in a list.

    Path lists must be checked element by element; a list-valued argument that
    skipped the checks would let any path through.
    """
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def _short(args: dict, limit: int = 160) -> str:
    parts = []
    for key, value in args.items():
        text = value if isinstance(value, str) else repr(value)
        parts.append(f"{key}={text[:60]}")
    text = ", ".join(parts)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# The longest summary a confirmation card shows. A summary that is cut says how
# much it hides, so the owner never taps a card that looks complete but is not.
_CARD_LIMIT = 600


def _clip(text: str) -> str:
    if len(text) <= _CARD_LIMIT:
        return text
    # The note is part of the limit, and its own digits change the count, so the
    # count is recomputed until the note states what the card really hides.
    hidden = len(text) - _CARD_LIMIT
    for _ in range(5):
        note = f" [{hidden} more characters not shown]"
        shown = text[: _CARD_LIMIT - len(note)]
        if len(text) - len(shown) == hidden:
            return shown + note
        hidden = len(text) - len(shown)
    return shown + note


class PolicyKernel:
    """Stateless. Safe to share between turns and threads."""

    def evaluate(
        self,
        spec: Optional["ToolSpec"],
        args: dict,
        ctx: CallContext,
        svc: Optional["Services"] = None,
    ) -> Verdict:
        if spec is None:
            return Verdict.deny("unknown_tool", "no such tool")
        try:
            return self._evaluate(spec, args, ctx, svc)
        except Exception:  # a broken rule must never become an allow
            log.exception("policy evaluation failed for %s", spec.name)
            return Verdict.deny("policy_error", "policy check failed; the action was refused")

    # ----------------------------------------------------------------- steps

    def _evaluate(self, spec: "ToolSpec", args: dict, ctx: CallContext, svc: Any) -> Verdict:
        # 2 - panic
        if ctx.autonomy == Autonomy.PANIC:
            return Verdict.deny("panic", "panic mode is on; nothing runs until it is resumed locally")

        # 3 - grants
        if spec.family not in ctx.grants:
            return Verdict.deny("not_granted", f"the «{spec.family}» family is turned off")

        # 4 - tiers that are never allowed
        if spec.tier in (Tier.FINANCIAL, Tier.CREDENTIAL):
            return Verdict.deny("prohibited_tier", "money and credential actions are never performed")

        # 5 - paths
        for name in spec.path_args:
            for value in _strings(args.get(name)):
                code = paths.check_path(value, write=spec.path_write)
                if code:
                    return Verdict.deny(code, f"{name} refused")

        # 6 - prohibited content (card numbers, key material) in any argument
        code = prohibited.scan_args(args)
        if code:
            return Verdict.deny(code, "the arguments contain something that must never leave or be typed")

        # 7 - surfaced-only arguments: a file may only be sent if this turn's
        #     owner-driven search returned it, or it was delivered before
        for name in spec.requires_surfaced:
            for value in _strings(args.get(name)):
                key = _key(value)
                if key not in ctx.surfaced and key not in ctx.delivered:
                    return Verdict.deny("not_surfaced", "that path did not come from a search in this conversation")

        # 8 - origin: a sensitive atom seen only in untrusted content. Reads are
        #     exempt: opening a link from a document is a normal request.
        if spec.sensitive_args and ctx.content_norm and (
            spec.tier in ORIGIN_TIERS or (spec.egress and ctx.local_read)
        ):
            # A path that one of this turn's searches returned is metadata the owner asked
            # for, so it is not "copied from content" even though the search result is marked
            # untrusted. Those arguments are checked for surfacing above, not for origin.
            atom = origin.first_content_only_atom(_without_owner_paths(args, spec.sensitive_args, ctx),
                                                  spec.sensitive_args, ctx)
            if atom:
                return Verdict.deny(
                    "origin_content",
                    "that value came from a document or page, not from the owner's own message",
                )

        # 9 - tier default, with the two kernel adjustments
        base = self._tier_default(spec, ctx)

        # 10 - relax hook: only for an owner turn under ask_for_writes. Under
        #      ask_always the owner asked to confirm every non-read action, so
        #      no tool may relax it; under AR and under content nothing runs.
        if (
            base == Decision.CONFIRM
            and spec.relax is not None
            and ctx.provenance == Provenance.OWNER
            and ctx.autonomy == Autonomy.ASK_FOR_WRITES
            and spec.relax(args, ctx)
        ):
            base = Decision.ALLOW
            base_code = "relaxed"
        else:
            base_code = "tier_default" if base != Decision.ALLOW else "allowed"
        verdicts = [Verdict(base, base_code, self._reason(spec, ctx, base))]

        # 11a - egress after a local read: a URL or query may carry the data just read
        if spec.egress and ctx.local_read:
            if ctx.autonomy == Autonomy.AUTONOMOUS_READONLY:
                verdicts.append(Verdict.deny("egress_unattended", "local data was read in this run; it may not leave the machine unattended"))
            else:
                verdicts.append(Verdict.confirm(
                    "egress_after_local_read",
                    "local data was read in this turn; this request could send it out",
                    summary=self._summary(spec, args),
                ))

        # 11 - taint escalation. File names and window titles (METADATA) are
        #      written by whoever named the file, so they escalate like content.
        if ctx.provenance >= Provenance.METADATA and self._taint_applies(spec):
            if ctx.autonomy == Autonomy.AUTONOMOUS_READONLY:
                verdicts.append(Verdict.deny("taint_unattended", "content in this run may not drive this action unattended"))
            elif base == Decision.ALLOW:
                verdicts.append(Verdict.confirm(
                    "taint_escalate",
                    "this action was suggested by content the owner did not write",
                    summary=self._summary(spec, args),
                ))

        # 12 - tool argument hooks
        for hook in spec.arg_checks:
            verdict = hook(args, ctx, svc)
            if verdict is not None:
                verdicts.append(verdict)

        # 13 - combine
        final = strictest(verdicts)
        if final.decision == Decision.CONFIRM and not final.summary:
            final = Verdict(final.decision, final.code, final.reason, self._summary(spec, args), final.two_channel)
        if final.decision == Decision.ALLOW:
            return Verdict(Decision.ALLOW, final.code, final.reason)
        return final

    # --------------------------------------------------------------- helpers

    def _tier_default(self, spec: "ToolSpec", ctx: CallContext) -> Decision:
        # The two exceptions come before the matrix lookup, so they hold under
        # AR too: notes and reminders are internal, and messages to the owner's
        # own chat are the point of a scheduled report. Panic is still total.
        if ctx.autonomy == Autonomy.PANIC:
            return Decision.DENY
        if spec.internal and spec.tier == Tier.LOCAL_WRITE and ctx.autonomy != Autonomy.ASK_ALWAYS:
            return Decision.ALLOW
        if spec.self_target and spec.tier == Tier.OUTBOUND and ctx.autonomy != Autonomy.ASK_ALWAYS:
            return Decision.ALLOW
        return matrix.tier_default(spec.tier, ctx.autonomy)

    @staticmethod
    def _taint_applies(spec: "ToolSpec") -> bool:
        # A message to the owner's own chat is the point of a report, so content
        # does not escalate it; any other outbound call does.
        if spec.tier == Tier.OUTBOUND:
            return not spec.self_target
        return spec.tier in matrix.TAINT_ESCALATED

    @staticmethod
    def _reason(spec: "ToolSpec", ctx: CallContext, decision: Decision) -> str:
        if decision == Decision.ALLOW:
            return ""
        return f"{spec.tier.value} actions need the owner's confirmation" if decision == Decision.CONFIRM else \
            f"{spec.tier.value} actions are not allowed in «{ctx.autonomy.value}» mode"

    @staticmethod
    def _summary(spec: "ToolSpec", args: dict) -> str:
        if spec.summary is not None:
            try:
                return _clip(spec.summary(args))
            except Exception:
                log.debug("summary hook failed for %s", spec.name, exc_info=True)
        return f"{spec.name}: {_short(args)}"
