"""Origin checks: a value copied from content is refused whatever its length.

A whole value is one atom, and a long value is also compared in 64-character
windows, so an instruction copied from a page is caught even when the model adds
a few words of its own around it.
"""
from __future__ import annotations

import pytest

from coworker.core.types import Autonomy, CallContext, Decision, Provenance, Tier, ToolResult, normalize_text
from coworker.policy import origin
from coworker.policy.kernel import PolicyKernel
from coworker.tools.registry import ToolSpec

KERNEL = PolicyKernel()
GRANTS = frozenset({"schedule", "desktop_control"})


def _instruction(words: int) -> str:
    """A sentence of distinct words, so that no two windows of it are equal."""
    return " ".join(f"step{i}name" for i in range(words))


def _ctx(content: str, owner: str = "schedule a daily report") -> CallContext:
    return CallContext(
        turn_id="turn-1",
        actor="owner",
        chat_id=42,
        autonomy=Autonomy.ASK_FOR_WRITES,
        grants=GRANTS,
        generation=0,
        provenance=Provenance.CONTENT,
        owner_norm=normalize_text(owner),
        content_norm=normalize_text(content),
    )


def _job_spec() -> ToolSpec:
    """Shaped like job_add: a SYSTEM_CHANGE tool whose instruction is origin-checked."""
    return ToolSpec(
        name="job_add",
        family="schedule",
        tier=Tier.SYSTEM_CHANGE,
        description="a test double for the scheduled-job tool",
        parameters={"type": "object", "properties": {}},
        handler=lambda call: ToolResult(ok=True),
        sensitive_args=("instruction",),
    )


# ------------------------------------------------------------------- whole values


def test_a_long_value_is_still_one_whole_value_atom():
    value = "x" * 581
    assert normalize_text(value) in origin.atoms(value)


def test_a_short_value_is_still_one_whole_value_atom():
    assert normalize_text("Send the report") in origin.atoms("Send the report")


def test_an_empty_or_blank_value_has_no_atoms():
    assert origin.atoms("") == set()
    assert origin.atoms("   ") == set()
    assert origin.atoms(None) == set()


def test_an_instruction_copied_from_a_page_is_refused_at_any_length():
    instruction = _instruction(120)  # about 1,000 characters, no URL, e-mail or path
    assert len(instruction) > 400
    verdict = KERNEL.evaluate(_job_spec(), {"instruction": instruction}, _ctx(f"notes: {instruction}"))
    assert (verdict.decision, verdict.code) == (Decision.DENY, "origin_content")


def test_a_long_instruction_the_owner_typed_is_not_refused():
    instruction = _instruction(120)
    ctx = _ctx(f"page says {instruction}", owner=f"please do this: {instruction}")
    assert origin.first_content_only_atom({"instruction": instruction}, ("instruction",), ctx) is None


# ----------------------------------------------------------------------- windows


def test_a_verbatim_passage_inside_a_longer_composed_value_is_refused():
    passage = _instruction(14)  # about 100 characters copied from a page
    composed = f"Every morning, then {passage}, and finally tidy the desk."
    verdict = KERNEL.evaluate(_job_spec(), {"instruction": composed}, _ctx(f"page: {passage}"))
    assert verdict.code == "origin_content"


def test_a_passage_the_owner_also_wrote_is_not_refused():
    passage = _instruction(14)
    composed = f"Every morning, then {passage}, and finally tidy the desk."
    ctx = _ctx(f"page: {passage}", owner=passage)
    assert origin.first_content_only_atom({"instruction": composed}, ("instruction",), ctx) is None


def test_windows_are_not_made_for_a_value_shorter_than_one_window():
    found = origin.atoms("short value of about forty characters")
    assert all(len(atom) >= origin.MIN_ATOM for atom in found)
    assert len(found) == 1


def test_a_window_is_64_characters_long():
    value = _instruction(20)
    assert origin.WINDOW == 64
    windows = [atom for atom in origin.atoms(value) if len(atom) == origin.WINDOW]
    assert windows
    assert all(window in normalize_text(value) for window in windows)


@pytest.mark.parametrize("offset", [0, 3, 17, 40])
def test_a_passage_copied_from_a_long_page_is_caught_at_any_offset(offset):
    page = _instruction(200)  # about 2,600 characters of page text
    passage = page[1000 + offset: 1100 + offset]
    composed = f"Do this: {passage} and then stop."
    assert origin.first_content_only_atom({"instruction": composed}, ("instruction",), _ctx(f"page: {page}")) is not None


def test_a_long_value_with_no_copied_passage_yields_no_content_only_atom():
    page = _instruction(200)
    composed = "Please write a short summary of the week for me, with three bullet points and no links at all."
    assert origin.first_content_only_atom({"instruction": composed}, ("instruction",), _ctx(f"page: {page}")) is None
