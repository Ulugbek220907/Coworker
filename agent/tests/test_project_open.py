"""Opening a project folder in an app: found by search, opened only from an exact shortcut.

Nothing is launched: the process start and the shortcut reader are replaced by fakes.
"""
from __future__ import annotations

import os

import pytest

from coworker import launcher
from coworker.core.types import Autonomy, CallContext, Decision, Provenance
from coworker.policy.kernel import PolicyKernel
from coworker.tools import apps, files
from coworker.tools.registry import Services, ToolCall


def _ctx(surfaced=frozenset()) -> CallContext:
    return CallContext(
        turn_id="t", actor="owner", chat_id=1, autonomy=Autonomy.ASK_FOR_WRITES,
        grants=frozenset({"apps", "files"}), generation=0, provenance=Provenance.OWNER,
        surfaced=frozenset(surfaced),
    )


def _spec(name: str, specs):
    return next(s for s in specs if s.name == name)


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "mini ai"
    folder.mkdir()
    return folder


def test_find_folder_returns_the_folder_and_surfaces_it(monkeypatch, project):
    monkeypatch.setattr(files.fs, "find_folders", lambda query, roots, **kw: {
        "query": query, "results": [{"name": "mini ai", "path": str(project), "score": 1.0}],
    })
    result = files._find_folder(ToolCall("find_folder", {"query": "mini ai"}, _ctx(), Services()))
    assert result.ok is True
    assert str(project) in result.surfaced


def test_find_folder_says_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(files.fs, "find_folders", lambda query, roots, **kw: {"query": query, "results": []})
    result = files._find_folder(ToolCall("find_folder", {"query": "nope"}, _ctx(), Services()))
    assert result.ok is True and result.surfaced == ()
    assert "topilmadi" in result.data["message"]


def test_the_folder_must_have_come_from_a_search(project):
    """A folder the model made up is refused by the kernel, before any app is touched."""
    spec = _spec("open_folder_in_app", apps.SPECS)
    verdict = PolicyKernel().evaluate(spec, {"app": "Antigravity", "folder": str(project)}, _ctx())
    assert verdict.decision == Decision.DENY
    assert verdict.code == "not_surfaced"


def test_an_app_that_is_not_an_exact_shortcut_is_refused(monkeypatch, project):
    monkeypatch.setattr(launcher, "resolve_exact", lambda name: None)
    started = []
    monkeypatch.setattr(apps.subprocess, "Popen", lambda *a, **k: started.append(a))
    result = apps._open_folder_in_app(ToolCall("open_folder_in_app",
                                               {"app": "Anti", "folder": str(project)}, _ctx(), Services()))
    assert result.ok is False
    assert started == [], "nothing starts for an app name that is not exact"


def test_the_folder_opens_through_the_exact_app_without_a_shell(monkeypatch, project):
    monkeypatch.setattr(launcher, "resolve_exact", lambda name: {"name": "Antigravity", "path": "x.lnk"})
    monkeypatch.setattr(apps, "_shortcut_target", lambda lnk: str(project / "Antigravity.exe"))
    monkeypatch.setattr(os.path, "isfile", lambda p: True)
    started = []
    monkeypatch.setattr(apps.subprocess, "Popen", lambda args, **kw: started.append((args, kw)))
    result = apps._open_folder_in_app(ToolCall("open_folder_in_app",
                                               {"app": "Antigravity", "folder": str(project)}, _ctx(), Services()))
    assert result.ok is True
    args, kw = started[0]
    assert args == [str(project / "Antigravity.exe"), str(project)]
    assert kw.get("shell", False) is False
    assert "ochildi" in result.data["message"]
