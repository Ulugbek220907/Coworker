"""The tier table: every cell, panic, and the two sets the kernel relies on."""
from __future__ import annotations

import pytest

from coworker.core.types import Autonomy, Decision, Tier
from coworker.policy import matrix

ALLOW, CONFIRM, DENY = Decision.ALLOW, Decision.CONFIRM, Decision.DENY
ASK_ALWAYS, ASK_FOR_WRITES, AUTONOMOUS = Autonomy.ASK_ALWAYS, Autonomy.ASK_FOR_WRITES, Autonomy.AUTONOMOUS_READONLY

# Written out cell by cell, so a change to the table shows up as a changed row here.
EXPECTED = {
    (Tier.READ, ASK_ALWAYS): ALLOW,
    (Tier.READ, ASK_FOR_WRITES): ALLOW,
    (Tier.READ, AUTONOMOUS): ALLOW,
    (Tier.LOCAL_WRITE, ASK_ALWAYS): CONFIRM,
    (Tier.LOCAL_WRITE, ASK_FOR_WRITES): CONFIRM,
    (Tier.LOCAL_WRITE, AUTONOMOUS): DENY,
    (Tier.DESTRUCTIVE, ASK_ALWAYS): CONFIRM,
    (Tier.DESTRUCTIVE, ASK_FOR_WRITES): CONFIRM,
    (Tier.DESTRUCTIVE, AUTONOMOUS): DENY,
    (Tier.OUTBOUND, ASK_ALWAYS): CONFIRM,
    (Tier.OUTBOUND, ASK_FOR_WRITES): CONFIRM,
    (Tier.OUTBOUND, AUTONOMOUS): DENY,
    (Tier.SYSTEM_CHANGE, ASK_ALWAYS): CONFIRM,
    (Tier.SYSTEM_CHANGE, ASK_FOR_WRITES): CONFIRM,
    (Tier.SYSTEM_CHANGE, AUTONOMOUS): DENY,
    (Tier.FINANCIAL, ASK_ALWAYS): DENY,
    (Tier.FINANCIAL, ASK_FOR_WRITES): DENY,
    (Tier.FINANCIAL, AUTONOMOUS): DENY,
    (Tier.CREDENTIAL, ASK_ALWAYS): DENY,
    (Tier.CREDENTIAL, ASK_FOR_WRITES): DENY,
    (Tier.CREDENTIAL, AUTONOMOUS): DENY,
}


@pytest.mark.parametrize("tier, autonomy", sorted(EXPECTED, key=lambda k: (k[0].value, k[1].value)))
def test_each_cell_of_the_table(tier, autonomy):
    assert matrix.tier_default(tier, autonomy) == EXPECTED[(tier, autonomy)]


@pytest.mark.parametrize("tier", list(Tier))
def test_panic_denies_every_tier_including_reads(tier):
    assert matrix.tier_default(tier, Autonomy.PANIC) == DENY


def test_the_table_covers_every_tier_and_every_non_panic_autonomy():
    assert set(matrix.MATRIX) == set(Tier)
    for row in matrix.MATRIX.values():
        assert set(row) == {ASK_ALWAYS, ASK_FOR_WRITES, AUTONOMOUS}


def test_taint_escalated_tiers_are_the_ones_content_can_drive():
    assert set(matrix.TAINT_ESCALATED) == {Tier.LOCAL_WRITE, Tier.DESTRUCTIVE, Tier.SYSTEM_CHANGE, Tier.OUTBOUND}


def test_reads_are_never_taint_escalated():
    assert Tier.READ not in matrix.TAINT_ESCALATED
