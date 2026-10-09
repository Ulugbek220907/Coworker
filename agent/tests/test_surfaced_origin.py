"""A folder the owner's own search returned is not "copied from content".

The search result is marked untrusted, so the turn becomes CONTENT. The path is still
the owner's: it was asked for by a search this turn. The same path that appeared only
in a document, never in a search result, is still refused.
"""
from __future__ import annotations

import os

from coworker.core.types import Autonomy, CallContext, Decision, Provenance, Tier, normalize_text
from coworker.policy.kernel import PolicyKernel
from coworker.tools.registry import ToolSpec

FOLDER = r"C:\Users\someone\Desktop\MiniAI"


def _spec(**fields) -> ToolSpec:
    base = dict(
        name="open_folder_in_app",
        family="apps",
        tier=Tier.SYSTEM_CHANGE,
        description="test",
        parameters={"type": "object", "properties": {"folder": {"type": "string"}}, "required": ["folder"]},
        handler=lambda call: None,
        path_args=("folder",),
        sensitive_args=("folder",),
    )
    base.update(fields)
    return ToolSpec(**base)


def _ctx(surfaced: frozenset, content: str, owner_driven: bool = True) -> CallContext:
    return CallContext(
        turn_id="t", actor="owner", chat_id=1, autonomy=Autonomy.ASK_FOR_WRITES,
        grants=frozenset({"apps"}), generation=0, provenance=Provenance.CONTENT,
        owner_norm=normalize_text("open the mini ai project"),
        content_norm=normalize_text(content),
        surfaced=surfaced,
        surfaced_owner=surfaced if owner_driven else frozenset(),
    )


def test_a_folder_returned_by_this_turns_search_passes_the_origin_check():
    key = os.path.normcase(os.path.normpath(FOLDER))
    ctx = _ctx(frozenset({key}), content=f"search result: {FOLDER}")
    verdict = PolicyKernel().evaluate(_spec(), {"folder": FOLDER}, ctx)
    assert verdict.code != "origin_content"
    assert verdict.decision in (Decision.ALLOW, Decision.CONFIRM)


def test_a_path_a_document_steered_the_search_to_is_still_refused():
    """The security case: the search is real, but the owner did not ask for it.

    A document says to find and send a file; the search returns it. The path is surfaced,
    yet it must not pass the origin check, or the document would have chosen the file.
    """
    key = os.path.normcase(os.path.normpath(FOLDER))
    ctx = _ctx(frozenset({key}), content=f"search result: {FOLDER}", owner_driven=False)
    verdict = PolicyKernel().evaluate(_spec(), {"folder": FOLDER}, ctx)
    assert verdict.decision == Decision.DENY
    assert verdict.code == "origin_content"


def test_the_same_path_seen_only_in_content_is_still_refused():
    ctx = _ctx(frozenset(), content=f"document says open {FOLDER} now")
    verdict = PolicyKernel().evaluate(_spec(), {"folder": FOLDER}, ctx)
    assert verdict.decision == Decision.DENY
    assert verdict.code == "origin_content"


def test_a_surfaced_value_does_not_hide_a_content_value_in_the_same_list():
    key = os.path.normcase(os.path.normpath(FOLDER))
    evil = r"C:\Users\someone\Desktop\Secrets"
    # Origin only: the path-argument rule is switched off so this test is about origin alone.
    spec = _spec(path_args=(), sensitive_args=("folder",))
    ctx = _ctx(frozenset({key}), content=f"{FOLDER} {evil}")
    verdict = PolicyKernel().evaluate(spec, {"folder": evil}, ctx)
    assert verdict.code == "origin_content"


def test_a_search_is_owner_driven_only_when_the_owner_asked_for_its_query():
    from coworker.core.types import CancelToken, ToolResult
    from coworker.tools.dispatch import TurnState

    spec = ToolSpec(name="find_folder", family="files", tier=Tier.READ, description="t",
                    parameters={"type": "object", "properties": {}}, handler=lambda call: None)
    result = ToolResult(ok=True, data={}, surfaced=(FOLDER,))

    owner = TurnState.new(1, "open the mini ai project", actor="owner", autonomy=Autonomy.ASK_FOR_WRITES,
                          grants=frozenset({"files"}), generation=0, cancel=CancelToken())
    owner.absorb(spec, result, {"query": "mini ai"})
    assert os.path.normcase(os.path.normpath(FOLDER)) in owner.surfaced_owner

    steered = TurnState.new(2, "open the mini ai project", actor="owner", autonomy=Autonomy.ASK_FOR_WRITES,
                            grants=frozenset({"files"}), generation=0, cancel=CancelToken())
    steered.absorb(spec, result, {"query": "secret"})
    assert steered.surfaced_owner == set(), "a query the owner never wrote does not make the result theirs"
    assert os.path.normcase(os.path.normpath(FOLDER)) in steered.surfaced
