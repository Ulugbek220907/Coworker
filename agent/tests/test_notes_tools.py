"""Notes, tasks and facts: the tool handlers, the trust flag on facts, and the LIKE search path.

The fake store follows the contract's method names. Its search is plain
case-insensitive substring matching, which is what the store's LIKE fallback
does, so these tests exercise the tool's behaviour when FTS5 is not used.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from coworker.core.types import Autonomy, CallContext, Decision, Provenance, Tier
from coworker.policy.kernel import PolicyKernel
from coworker.store.db import Store
from coworker.tools import notes as notes_module
from coworker.tools.notes import NOTE_VIEW_CHARS, SPECS
from coworker.tools.registry import Services, ToolCall, validate_args

NOW = datetime(2026, 10, 8, 8, 0).timestamp()
SPEC = {spec.name: spec for spec in SPECS}


@pytest.fixture(autouse=True)
def coworker_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_HOME", str(tmp_path))


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(notes_module, "time", SimpleNamespace(time=lambda: NOW))
    return NOW


def _contains(value, needle: str) -> bool:
    return needle.casefold() in (value or "").casefold()


class FakeStore:
    """In-memory rows keyed by chat, with LIKE-style substring search."""

    def __init__(self) -> None:
        self.kv: dict = {}
        self.facts: dict[int, dict] = {}
        self.notes: dict[int, dict] = {}
        self.tasks: dict[int, dict] = {}
        self.last_query: str | None = None
        self._next = 0

    def _id(self) -> int:
        self._next += 1
        return self._next

    def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    def fact_add(self, chat_id, text, *, kind="note", untrusted=False):
        fid = self._id()
        self.facts[fid] = {"id": fid, "chat_id": chat_id, "text": text, "kind": kind, "untrusted": untrusted}
        return fid

    def facts_search(self, chat_id, query, limit=10):
        self.last_query = query
        rows = [dict(f) for f in self.facts.values() if f["chat_id"] == chat_id and _contains(f["text"], query)]
        return rows[:limit]

    def fact_forget(self, chat_id, needle):
        doomed = [fid for fid, f in self.facts.items() if f["chat_id"] == chat_id and _contains(f["text"], needle)]
        for fid in doomed:
            del self.facts[fid]
        return len(doomed)

    def note_add(self, chat_id, title, body):
        nid = self._id()
        self.notes[nid] = {"id": nid, "chat_id": chat_id, "title": title, "body": body}
        return nid

    def notes_search(self, chat_id, query, limit=10):
        self.last_query = query
        rows = [
            dict(n) for n in self.notes.values()
            if n["chat_id"] == chat_id and (_contains(n["title"], query) or _contains(n["body"], query))
        ]
        return rows[:limit]

    def notes_list(self, chat_id, limit=20):
        rows = [dict(n) for n in self.notes.values() if n["chat_id"] == chat_id]
        return list(reversed(rows))[:limit]

    def task_add(self, chat_id, text, due_ts=None):
        tid = self._id()
        self.tasks[tid] = {"id": tid, "chat_id": chat_id, "text": text, "due_ts": due_ts, "done": False}
        return tid

    def tasks_list(self, chat_id, open_only=True):
        return [
            dict(t) for t in self.tasks.values()
            if t["chat_id"] == chat_id and (not open_only or not t["done"])
        ]

    def task_done(self, chat_id, tid):
        row = self.tasks.get(tid)
        if row is None or row["chat_id"] != chat_id or row["done"]:
            return False
        row["done"] = True
        return True


def run(name: str, args: dict, store: FakeStore, *, chat_id=42, provenance=Provenance.OWNER):
    ctx = CallContext(
        turn_id="t1", actor="owner", chat_id=chat_id, autonomy=Autonomy.ASK_FOR_WRITES,
        grants=frozenset({"notes"}), generation=0, provenance=provenance,
    )
    return SPEC[name].handler(ToolCall(name=name, args=args, ctx=ctx, svc=Services(store=store)))


# ------------------------------------------------------------- specs and flags

EXPECTED_FLAGS = {
    "note_add": (Tier.LOCAL_WRITE, True),
    "note_search": (Tier.READ, False),
    "note_list": (Tier.READ, False),
    "task_add": (Tier.LOCAL_WRITE, True),
    "task_list": (Tier.READ, False),
    "task_done": (Tier.LOCAL_WRITE, True),
    "remember": (Tier.LOCAL_WRITE, True),
    "recall": (Tier.READ, False),
    "forget_fact": (Tier.LOCAL_WRITE, True),
}


def test_every_notes_row_of_the_catalogue_is_present_with_its_tier_and_flag():
    assert set(SPEC) == set(EXPECTED_FLAGS)
    for name, (tier, internal) in EXPECTED_FLAGS.items():
        assert SPEC[name].family == "notes", name
        assert SPEC[name].tier == tier, name
        assert SPEC[name].internal is internal, name
        assert SPEC[name].gov_class == "TOOL", name


def test_no_notes_tool_is_marked_untrusted_because_recall_decides_per_result():
    assert not any(spec.untrusted for spec in SPECS)


# ------------------------------------------------------------ facts and trust

@pytest.mark.parametrize("provenance, untrusted", [
    (Provenance.OWNER, False),
    (Provenance.METADATA, True),
    (Provenance.CONTENT, True),
])
def test_remember_stores_untrusted_only_when_the_turn_has_seen_content(provenance, untrusted):
    store = FakeStore()
    result = run("remember", {"fact": "Shartnoma 12-mart"}, store, provenance=provenance)
    assert result.ok
    assert store.facts[result.data["id"]]["untrusted"] is untrusted
    assert result.data["untrusted"] is untrusted


def test_recall_is_untrusted_when_any_returned_fact_is_untrusted():
    store = FakeStore()
    store.fact_add(42, "budget: 500 dollar", untrusted=True)
    store.fact_add(42, "budget is owner's", untrusted=False)
    result = run("recall", {"query": "budget"}, store)
    assert result.untrusted is True
    flags = {f["text"]: f["untrusted"] for f in result.data["facts"]}
    assert flags == {"budget: 500 dollar": True, "budget is owner's": False}


def test_recall_of_owner_facts_alone_is_trusted():
    store = FakeStore()
    store.fact_add(42, "dushanba kuni yig'ilish", untrusted=False)
    result = run("recall", {"query": "yig'ilish"}, store)
    assert result.untrusted is False
    assert result.data["facts"] == [{"id": 1, "text": "dushanba kuni yig'ilish", "untrusted": False}]


def test_recall_with_no_match_is_ok_and_empty():
    result = run("recall", {"query": "nothing"}, FakeStore())
    assert result.ok and result.data["facts"] == [] and result.untrusted is False


def test_remember_refuses_a_blank_fact():
    result = run("remember", {"fact": "   "}, FakeStore())
    assert result.code == "arg_invalid"


def test_forget_fact_refuses_a_needle_too_short_to_be_safe():
    store = FakeStore()
    store.fact_add(42, "budget is 500")
    result = run("forget_fact", {"needle": "a"}, store)
    assert result.code == "arg_invalid"
    assert len(store.facts) == 1


def test_forget_fact_reports_how_many_facts_it_removed():
    store = FakeStore()
    store.fact_add(42, "budget is 500")
    store.fact_add(42, "budget review on friday")
    store.fact_add(42, "unrelated")
    result = run("forget_fact", {"needle": "budget"}, store)
    assert result.ok and result.data == {"removed": 2}
    assert [f["text"] for f in store.facts.values()] == ["unrelated"]


# -------------------------------------------------------- note search and lists

def test_note_search_passes_plain_text_through_and_matches_by_substring():
    store = FakeStore()
    store.note_add(42, "Shartnoma", 'Ali "contract" * draft')
    store.note_add(42, "Ovqat", "non va sut")
    query = 'contract" *'
    result = run("note_search", {"query": query}, store)

    assert store.last_query == query
    assert [n["title"] for n in result.data["notes"]] == ["Shartnoma"]


def test_note_search_is_case_insensitive_through_the_fallback():
    store = FakeStore()
    store.note_add(42, "Hisobot", "Yanvar oyi natijasi")
    result = run("note_search", {"query": "YANVAR"}, store)
    assert [n["title"] for n in result.data["notes"]] == ["Hisobot"]


def test_note_search_clips_long_bodies_for_the_model():
    store = FakeStore()
    store.note_add(42, "uzun", "x" * (NOTE_VIEW_CHARS + 500))
    body = run("note_search", {"query": "uzun"}, store).data["notes"][0]["body"]
    assert len(body) == NOTE_VIEW_CHARS


def test_note_search_refuses_a_blank_query():
    assert run("note_search", {"query": "  "}, FakeStore()).code == "arg_invalid"


def test_note_add_then_list_returns_the_saved_note():
    store = FakeStore()
    added = run("note_add", {"title": "Yig'ilish", "body": "dushanba 10:00"}, store)
    assert added.ok
    listed = run("note_list", {}, store).data["notes"]
    assert listed == [{"id": added.data["id"], "title": "Yig'ilish", "body": "dushanba 10:00"}]


def test_note_add_refuses_a_blank_title():
    result = run("note_add", {"title": " ", "body": "matn"}, FakeStore())
    assert result.code == "arg_invalid"


def test_note_list_limit_is_bounded_by_the_schema():
    params = SPEC["note_list"].parameters
    assert validate_args(params, {"limit": 0})
    assert validate_args(params, {"limit": 51})
    assert validate_args(params, {"limit": 50}) is None


# ------------------------------------------------------------------- tasks

def test_task_add_with_a_relative_due_stores_the_due_time(clock):
    store = FakeStore()
    result = run("task_add", {"text": "hisobot yubor", "due": "+1h"}, store)
    assert result.ok
    assert store.tasks[result.data["id"]]["due_ts"] == NOW + 3600
    assert result.data["due"] == "2026-10-08T09:00"


def test_task_add_without_a_due_stores_none(clock):
    store = FakeStore()
    result = run("task_add", {"text": "kitob o'qi"}, store)
    assert store.tasks[result.data["id"]]["due_ts"] is None
    assert result.data["due"] is None


def test_task_add_refuses_a_due_time_already_past(clock):
    store = FakeStore()
    result = run("task_add", {"text": "eski", "due": "2026-10-01T10:00"}, store)
    assert result.code == "arg_invalid"
    assert store.tasks == {}


def test_task_list_hides_done_tasks_unless_asked(clock):
    store = FakeStore()
    first = run("task_add", {"text": "birinchi"}, store).data["id"]
    run("task_add", {"text": "ikkinchi"}, store)
    run("task_done", {"id": first}, store)

    open_tasks = run("task_list", {}, store).data["tasks"]
    assert [t["text"] for t in open_tasks] == ["ikkinchi"]

    all_tasks = run("task_list", {"open_only": False}, store).data["tasks"]
    assert {t["text"]: t["done"] for t in all_tasks} == {"birinchi": True, "ikkinchi": False}


def test_task_done_refuses_an_unknown_or_finished_id(clock):
    store = FakeStore()
    tid = run("task_add", {"text": "x"}, store).data["id"]
    assert run("task_done", {"id": tid}, store).ok
    assert run("task_done", {"id": tid}, store).code == "arg_invalid"
    assert run("task_done", {"id": 999}, store).code == "arg_invalid"


# --------------------------------------------------------- owner and scheduled runs

def test_writes_before_pairing_are_refused_not_guessed():
    store = FakeStore()
    result = run("note_add", {"title": "x", "body": "y"}, store, chat_id=None)
    assert result.code == "not_configured"
    assert store.notes == {}


def test_a_scheduled_run_with_no_chat_writes_to_the_paired_owner(clock):
    store = FakeStore()
    store.kv["owner_chat_id"] = 42
    result = run("remember", {"fact": "har juma hisobot"}, store, chat_id=None)
    assert result.ok
    assert store.facts[result.data["id"]]["chat_id"] == 42


def test_one_owners_notes_are_invisible_to_another_chat():
    store = FakeStore()
    run("note_add", {"title": "sir", "body": "maxfiy"}, store, chat_id=7)
    assert run("note_search", {"query": "maxfiy"}, store, chat_id=42).data["notes"] == []


# ----------------------------------------------------------- kernel decisions

def kernel_ctx(autonomy: Autonomy, provenance: Provenance = Provenance.OWNER) -> CallContext:
    return CallContext(
        turn_id="t1", actor="owner", chat_id=42, autonomy=autonomy,
        grants=frozenset({"notes"}), generation=0, provenance=provenance,
    )


def test_the_kernel_lets_the_owner_save_notes_without_a_tap():
    verdict = PolicyKernel().evaluate(SPEC["note_add"], {"title": "t", "body": "b"}, kernel_ctx(Autonomy.ASK_FOR_WRITES))
    assert verdict.decision == Decision.ALLOW


def test_a_fact_saved_after_content_was_read_needs_a_tap():
    ctx = kernel_ctx(Autonomy.ASK_FOR_WRITES, Provenance.CONTENT)
    verdict = PolicyKernel().evaluate(SPEC["remember"], {"fact": "budget 500"}, ctx)
    assert verdict.decision == Decision.CONFIRM
    assert verdict.code == "taint_escalate"


def test_internal_notes_are_allowed_unattended():
    verdict = PolicyKernel().evaluate(SPEC["note_add"], {"title": "t", "body": "b"}, kernel_ctx(Autonomy.AUTONOMOUS_READONLY))
    assert verdict.decision == Decision.ALLOW


# ------------------------------------------------------- against the real store

@pytest.fixture
def real_store(tmp_path):
    """The real store with FTS5 off, so notes and facts go through the scan fallback."""
    store = Store(tmp_path / "coworker.db", use_fts=False)
    assert store.fts_enabled is False
    return store


def test_note_search_through_the_real_scan_fallback(real_store):
    run("note_add", {"title": "Shartnoma", "body": 'Ali "contract" * draft'}, real_store)
    run("note_add", {"title": "Ovqat", "body": "non va sut"}, real_store)

    hits = run("note_search", {"query": 'contract" *'}, real_store).data["notes"]
    assert [n["title"] for n in hits] == ["Shartnoma"]
    assert run("note_search", {"query": "draft missing"}, real_store).data["notes"] == []


def test_recall_flags_untrusted_facts_read_back_from_the_real_store(real_store):
    run("remember", {"fact": "budget comes from content"}, real_store, provenance=Provenance.CONTENT)
    run("remember", {"fact": "budget is the owner's own"}, real_store)

    result = run("recall", {"query": "budget"}, real_store)
    assert result.untrusted is True
    flags = {f["text"]: f["untrusted"] for f in result.data["facts"]}
    assert flags == {"budget comes from content": True, "budget is the owner's own": False}
