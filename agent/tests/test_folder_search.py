"""Folder search by name: spaces, hyphens and underscores do not separate a spoken name from a folder.

Each test builds its own temporary tree. The well-known folders are stubbed out, so the
result depends only on the folders the test created.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coworker import fs, textutil


@pytest.fixture(autouse=True)
def _no_known_folders(monkeypatch):
    monkeypatch.setattr(fs, "known_folders", lambda **_: {})


def _tree(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / "root"
    for name in names:
        (root / name).mkdir(parents=True)
    return root


def _names(query: str, root: Path) -> list[str]:
    return [r["name"] for r in fs.find_folders(query, [str(root)], min_score=0.5)["results"]]


@pytest.mark.parametrize("query", ["mini ai", "MiniAI", "mini-ai", "mini_ai", "MINI AI", "miniai"])
def test_mini_ai_folder_is_found_whatever_the_separators(tmp_path, query):
    # "Mini Tools" shares one word, so it may follow at half credit; MiniAI must rank first.
    root = _tree(tmp_path, "MiniAI", "Mini Tools")
    names = _names(query, root)
    assert names[0] == "MiniAI"
    assert "MiniAI" in names


def test_the_squashed_match_is_a_full_hit(tmp_path):
    root = _tree(tmp_path, "MiniAI")
    results = fs.find_folders("mini ai", [str(root)], min_score=0.5)["results"]
    assert results[0]["name"] == "MiniAI" and results[0]["score"] == 1.0


def test_a_longer_folder_name_that_begins_with_the_squashed_query_is_found(tmp_path):
    root = _tree(tmp_path, "MiniAI Project", "Other")
    assert _names("mini ai", root) == ["MiniAI Project"]


def test_a_folder_sharing_no_word_with_the_query_is_not_found(tmp_path):
    root = _tree(tmp_path, "Tools Box")
    assert _names("mini ai", root) == []


def test_a_folder_named_with_only_filler_words_is_found(tmp_path):
    root = _tree(tmp_path, "Ok")
    assert _names("ok", root) == ["Ok"]


def test_a_stopword_only_name_keeps_its_words_in_textutil():
    assert textutil.tokens("ha") == ["ha"]
    assert textutil.tokens("the file") == ["the", "file"]
    assert textutil.score("ok", "Ok") == 1.0


def test_filler_words_are_still_dropped_next_to_a_real_word():
    assert textutil.tokens("ok report") == ["report"]


def test_a_single_letter_still_names_nothing():
    assert textutil.tokens("a") == []
    assert textutil.score("a", "a.txt") == 0.0
