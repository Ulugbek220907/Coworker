"""The default decision for each tier under each autonomy level.

Read this table first when a question comes up about what the assistant may do
unattended. Everything else in the kernel can only make a decision stricter,
except for the explicit relax hooks on individual tools.

                      ask_always  ask_for_writes  autonomous_readonly
    READ                ALLOW         ALLOW            ALLOW
    LOCAL_WRITE         CONFIRM       CONFIRM          DENY
    DESTRUCTIVE         CONFIRM       CONFIRM          DENY
    OUTBOUND            CONFIRM       CONFIRM          DENY
    SYSTEM_CHANGE       CONFIRM       CONFIRM          DENY
    FINANCIAL           DENY          DENY             DENY
    CREDENTIAL          DENY          DENY             DENY
    (panic: everything DENY)

Two adjustments are made by the kernel, not here:
  * an internal LOCAL_WRITE (notes, facts, reminders) is ALLOW under AW and AR;
  * an OUTBOUND to the owner's own chat is ALLOW under AW and AR.
"""
from __future__ import annotations

from ..core.types import Autonomy, Decision, Tier

_A = Autonomy
_D = Decision

MATRIX: dict[Tier, dict[Autonomy, Decision]] = {
    Tier.READ: {_A.ASK_ALWAYS: _D.ALLOW, _A.ASK_FOR_WRITES: _D.ALLOW, _A.AUTONOMOUS_READONLY: _D.ALLOW},
    Tier.LOCAL_WRITE: {_A.ASK_ALWAYS: _D.CONFIRM, _A.ASK_FOR_WRITES: _D.CONFIRM, _A.AUTONOMOUS_READONLY: _D.DENY},
    Tier.DESTRUCTIVE: {_A.ASK_ALWAYS: _D.CONFIRM, _A.ASK_FOR_WRITES: _D.CONFIRM, _A.AUTONOMOUS_READONLY: _D.DENY},
    Tier.OUTBOUND: {_A.ASK_ALWAYS: _D.CONFIRM, _A.ASK_FOR_WRITES: _D.CONFIRM, _A.AUTONOMOUS_READONLY: _D.DENY},
    Tier.SYSTEM_CHANGE: {_A.ASK_ALWAYS: _D.CONFIRM, _A.ASK_FOR_WRITES: _D.CONFIRM, _A.AUTONOMOUS_READONLY: _D.DENY},
    Tier.FINANCIAL: {_A.ASK_ALWAYS: _D.DENY, _A.ASK_FOR_WRITES: _D.DENY, _A.AUTONOMOUS_READONLY: _D.DENY},
    Tier.CREDENTIAL: {_A.ASK_ALWAYS: _D.DENY, _A.ASK_FOR_WRITES: _D.DENY, _A.AUTONOMOUS_READONLY: _D.DENY},
}

# Tiers whose content taint escalates a permitted action to a confirmation.
TAINT_ESCALATED = (Tier.LOCAL_WRITE, Tier.DESTRUCTIVE, Tier.SYSTEM_CHANGE, Tier.OUTBOUND)


def tier_default(tier: Tier, autonomy: Autonomy) -> Decision:
    """The table lookup. Panic denies everything, including reads."""
    if autonomy == Autonomy.PANIC:
        return Decision.DENY
    return MATRIX[tier][autonomy]
