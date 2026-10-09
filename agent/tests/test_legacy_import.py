"""Legacy import: old per-chat JSON moves into the store once, and each source is renamed."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coworker.store import Store
from coworker.store import legacy_import
from coworker.store.legacy_import import MIGRATED_SUFFIX, import_legacy_chats

LEGACY = {
    "turns": [
        {"role": "user", "content": "salom", "ts": 100.0},
        {"role": "assistant", "content": "Vaalaykum", "ts": 101.0},
        {"role": "user", "content": "", "ts": 102.0},
    ],
    "summary": "Foydalanuvchi shartnomalar haqida so'radi.",
    "facts": [
        {"text": "shartnomalar D:/Ishxona/Shartnomalar ichida", "kind": "note", "ts": 90.0},
        {"text": "   ", "kind": "note", "ts": 91.0},
    ],
    "delivered": [
        {"path": "D:/a.pdf", "name": "a.pdf", "ts": 80.0},
        {"path": "D:/b.pdf", "name": "b.pdf", "ts": 81.0},
        {"path": "D:/a.pdf", "name": "a.pdf", "ts": 82.0},
    ],
}


def _chats(home: Path) -> Path:
    folder = home / "chats"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _write(folder: Path, name: str, payload: object) -> Path:
    path = folder / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_a_legacy_chat_lands_in_every_table(store: Store, isolated_home: Path) -> None:
    src = _write(_chats(isolated_home), "555.json", LEGACY)
    assert import_legacy_chats(store) == 1
    assert [t["content"] for t in store.turns_recent(555)] == ["salom", "Vaalaykum"]
    assert store.summary_get(555).startswith("Foydalanuvchi")
    facts = store.facts_list(555)
    assert [f["text"] for f in facts] == ["shartnomalar D:/Ishxona/Shartnomalar ichida"]
    assert facts[0]["untrusted"] is True
    assert [d["path"] for d in store.delivered_recent(555)] == ["D:/a.pdf", "D:/b.pdf"]
    assert store.facts_search(555, "ishxona")  # the FTS trigger indexed the imported fact
    assert not src.exists()
    assert (src.parent / f"555.json{MIGRATED_SUFFIX}").exists()


def test_a_second_run_imports_nothing_and_duplicates_nothing(store: Store, isolated_home: Path) -> None:
    _write(_chats(isolated_home), "555.json", LEGACY)
    assert import_legacy_chats(store) == 1
    assert import_legacy_chats(store) == 0
    assert len(store.turns_recent(555, limit=100)) == 2
    assert len(store.facts_list(555)) == 1


def test_a_crash_before_the_rename_does_not_import_twice(
    store: Store, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _chats(isolated_home)
    src = _write(folder, "555.json", LEGACY)
    real_retire = legacy_import._retire
    monkeypatch.setattr(legacy_import, "_retire", lambda path: None)  # simulate a crash here
    assert import_legacy_chats(store) == 1
    assert src.exists()
    # Restore only this function: undoing everything would also drop the COWORKER_HOME
    # override and point the import at the real user folder.
    monkeypatch.setattr(legacy_import, "_retire", real_retire)
    assert import_legacy_chats(store) == 0
    assert len(store.turns_recent(555, limit=100)) == 2
    assert not src.exists()


def test_each_chat_is_imported_on_its_own(store: Store, isolated_home: Path) -> None:
    folder = _chats(isolated_home)
    _write(folder, "1.json", {"turns": [{"role": "user", "content": "a"}]})
    _write(folder, "2.json", {"summary": "b"})
    assert import_legacy_chats(store) == 2
    assert store.turns_recent(1)[0]["content"] == "a"
    assert store.summary_get(2) == "b"
    assert store.turns_recent(2) == []


def test_unreadable_and_odd_files_are_left_in_place(store: Store, isolated_home: Path) -> None:
    folder = _chats(isolated_home)
    (folder / "888.json").write_text("{not json", encoding="utf-8")
    _write(folder, "777.json", [1, 2])
    _write(folder, "notes.json", {"turns": []})
    assert import_legacy_chats(store) == 0
    for name in ("888.json", "777.json", "notes.json"):
        assert (folder / name).exists()


def test_a_missing_chats_folder_is_not_an_error(store: Store, isolated_home: Path) -> None:
    assert import_legacy_chats(store) == 0
    assert not (isolated_home / "chats").exists()


def test_files_written_by_the_old_memory_module_import(store: Store, isolated_home: Path) -> None:
    # The layout the retired per-chat memory wrote: one JSON file per chat id.
    folder = isolated_home / "chats"
    folder.mkdir(parents=True)
    (folder / "777.json").write_text(json.dumps({
        "turns": [{"role": "user", "content": "salom", "ts": 1.0}],
        "facts": [{"text": "Zavod Tekstil", "kind": "note", "ts": 1.0}],
        "summary": "",
        "delivered": [{"path": "D:/x.pdf", "name": "x.pdf", "ts": 1.0}],
    }, ensure_ascii=False), encoding="utf-8")
    assert import_legacy_chats(store) == 1
    assert store.turns_recent(777)[0]["content"] == "salom"
    assert [f["text"] for f in store.facts_list(777)] == ["Zavod Tekstil"]
    assert [d["path"] for d in store.delivered_recent(777)] == ["D:/x.pdf"]
