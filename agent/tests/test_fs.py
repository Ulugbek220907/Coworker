"""Filesystem primitives: allowlist, error codes, deduplication, snippets and COM memory."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path

from coworker import fs


def _touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------ error codes

def test_missing_folder_returns_path_invalid(tmp_path):
    result = fs.list_dir(str(tmp_path / "nope"))
    assert result["code"] == "path_invalid"
    assert "error" in result


def test_file_where_a_folder_is_needed_returns_arg_invalid(tmp_path):
    file = _touch(tmp_path / "a.txt")
    assert fs.list_dir(str(file))["code"] == "arg_invalid"


def test_preview_of_missing_file_returns_path_invalid(tmp_path):
    result = fs.preview_file(str(tmp_path / "gone.docx"))
    assert result["code"] == "path_invalid"


def test_preview_of_a_folder_returns_arg_invalid(tmp_path):
    assert fs.preview_file(str(tmp_path))["code"] == "arg_invalid"


def test_successful_listing_has_no_error_key(tmp_path):
    _touch(tmp_path / "a.txt")
    result = fs.list_dir(str(tmp_path))
    assert "error" not in result and "code" not in result
    assert [f["path"] for f in result["files"]] == [str(tmp_path / "a.txt")]


# ------------------------------------------------------------ allowlist

def test_list_dir_outside_the_allowlist_is_refused(tmp_path):
    inside = tmp_path / "allowed"
    inside.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    result = fs.list_dir(str(outside), allowed_roots=[str(inside)])
    assert result["code"] == "path_invalid"


def test_list_dir_inside_the_allowlist_works(tmp_path):
    inside = tmp_path / "allowed"
    _touch(inside / "a.txt")
    assert fs.list_dir(str(inside), allowed_roots=[str(inside)])["files"]


def test_find_files_confined_to_an_allowed_folder(tmp_path):
    keep = _touch(tmp_path / "keep" / "Shartnoma.docx")
    _touch(tmp_path / "other" / "Shartnoma2.docx")
    found = fs.find_files("shartnoma", [str(tmp_path)], allowed_roots=[str(tmp_path / "keep")])
    assert [r["path"] for r in found["results"]] == [str(keep)]


def test_root_that_contains_an_allowed_folder_is_narrowed_to_it(tmp_path):
    keep = _touch(tmp_path / "keep" / "Shartnoma.docx")
    _touch(tmp_path / "other" / "Shartnoma3.docx")
    # The root is the parent; only the allowed child may be walked.
    found = fs.find_files("shartnoma", [str(tmp_path)], allowed_roots=[str(tmp_path / "keep")])
    paths = [r["path"] for r in found["results"]]
    assert paths == [str(keep)]


def test_root_outside_the_allowlist_is_dropped(tmp_path):
    _touch(tmp_path / "elsewhere" / "Shartnoma.docx")
    found = fs.find_files("shartnoma", [str(tmp_path / "elsewhere")], allowed_roots=[str(tmp_path / "allowed")])
    assert found["results"] == []


def test_recent_files_respects_the_allowlist(tmp_path):
    _touch(tmp_path / "allowed" / "new.docx")
    _touch(tmp_path / "blocked" / "new.docx")
    result = fs.recent_files([str(tmp_path)], days=1, allowed_roots=[str(tmp_path / "allowed")])
    assert [r["path"] for r in result["results"]] == [str(tmp_path / "allowed" / "new.docx")]


def test_search_in_files_skips_candidates_outside_the_allowlist(tmp_path):
    inside = _touch(tmp_path / "allowed" / "a.txt", "shartnoma matn")
    outside = _touch(tmp_path / "blocked" / "b.txt", "shartnoma matn")
    result = fs.search_in_files("shartnoma", [str(inside), str(outside)], allowed_roots=[str(tmp_path / "allowed")])
    assert [r["path"] for r in result["results"]] == [str(inside)]


# ----------------------------------------------------------- deduplication

def test_overlapping_roots_return_each_file_once(tmp_path):
    # The priority folder is walked first; the parent root is not inside it, so the
    # parent is walked too and reaches the same file a second time.
    shared = _touch(tmp_path / "OneDrive" / "Desktop" / "Shartnoma_2024.docx")
    result = fs.find_files(
        "shartnoma", [str(tmp_path / "OneDrive")], priority=[str(tmp_path / "OneDrive" / "Desktop")],
    )
    assert [r["path"] for r in result["results"]] == [str(shared)]
    assert result["scanned"] == 1


def test_priority_folder_inside_a_root_is_not_scored_twice(tmp_path):
    _touch(tmp_path / "root" / "sub" / "Shartnoma.docx")
    result = fs.find_files(
        "shartnoma", [str(tmp_path / "root")], priority=[str(tmp_path / "root" / "sub")],
    )
    assert len(result["results"]) == 1
    assert result["scanned"] == 1


def test_recent_files_are_deduplicated_across_overlapping_roots(tmp_path):
    _touch(tmp_path / "root" / "sub" / "new.txt")
    result = fs.recent_files([str(tmp_path / "root")], days=1, priority=[str(tmp_path / "root" / "sub")])
    assert len(result["results"]) == 1


def test_find_folders_does_not_repeat_a_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "known_folders", lambda **_: {})
    (tmp_path / "root" / "Shartnoma").mkdir(parents=True)
    result = fs.find_folders("shartnoma", [str(tmp_path / "root"), str(tmp_path / "root")], min_score=0.5)
    paths = [r["path"] for r in result["results"]]
    assert len(paths) == 1 and len(paths) == len(set(paths))


# ------------------------------------------------------------ matching

def test_ma_pdf_is_not_found_by_a_longer_query(tmp_path):
    _touch(tmp_path / "Ma.pdf")
    assert fs.find_files("malika", [str(tmp_path)])["results"] == []
    assert [r["name"] for r in fs.find_files("ma", [str(tmp_path)])["results"]] == ["Ma.pdf"]


# ------------------------------------------------------------ snippets

def test_snippet_points_at_the_matched_words_after_a_lengthening_fold():
    # "Ё" folds to "yo". Eighty of them push the match 80 characters further along in the
    # folded text than in the original, which is well past a 60-character window's margin.
    text = "Ё" * 80 + " Договор на поставку товаров и услуг"
    snippet = fs._snippet(text, "dogovor", 60)
    assert "Договор" in snippet


def test_snippet_from_search_in_files_contains_the_real_word(tmp_path):
    path = _touch(tmp_path / "note.txt", "Ё" * 80 + " Договор на поставку")
    result = fs.search_in_files("dogovor", [str(path)])
    assert result["results"]
    assert "Договор" in result["results"][0]["snippet"]


# --------------------------------------------------------- COM memory

class _FakeShell32:
    """Writes a COM-style pointer for each known folder, as SHGetKnownFolderPath does."""

    def __init__(self, paths: dict[str, str], failing: set[str] = frozenset()):
        self.paths = paths
        self.failing = set(failing)
        self.buffers: list = []
        self.guid_order = list(fs._KNOWN_FOLDER_GUIDS.values())

    def SHGetKnownFolderPath(self, rguid, flags, token, out):
        import uuid

        name = next(n for n, g in fs._KNOWN_FOLDER_GUIDS.items() if uuid.UUID(g).bytes_le == bytes(rguid._obj))
        value = self.paths.get(name, "")
        buf = ctypes.create_unicode_buffer(value)
        self.buffers.append(buf)
        out.contents.value = ctypes.addressof(buf)
        return -2147024894 if name in self.failing else 0  # E_FAIL, after writing the pointer


class _FakeOle32:
    def __init__(self):
        self.freed: list[int] = []

    def CoTaskMemFree(self, pointer):
        self.freed.append(pointer.value)


def test_known_folder_path_is_released_with_co_task_mem_free(tmp_path):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    shell32 = _FakeShell32({"Desktop": str(desktop), "Documents": str(tmp_path / "missing")})
    ole32 = _FakeOle32()
    folders = fs.known_folders(shell32=shell32, ole32=ole32)
    assert folders == {"Desktop": str(desktop)}
    # Every lookup allocates one string, and every one is released, including the one
    # for a folder that does not exist.
    assert len(ole32.freed) == len(shell32.buffers) == len(fs._KNOWN_FOLDER_GUIDS)
    assert set(ole32.freed) == {ctypes.addressof(b) for b in shell32.buffers}


def test_known_folder_path_is_released_when_the_call_fails(tmp_path):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    shell32 = _FakeShell32({"Desktop": str(desktop)}, failing={"Desktop"})
    ole32 = _FakeOle32()
    assert "Desktop" not in fs.known_folders(shell32=shell32, ole32=ole32)
    assert ctypes.addressof(shell32.buffers[0]) in ole32.freed


# ------------------------------------------------------------ walks

def test_plain_folder_is_not_treated_as_a_link(tmp_path):
    _touch(tmp_path / "plain" / "a.txt")
    entry = next(e for e in os.scandir(tmp_path) if e.name == "plain")
    assert fs.is_link_entry(entry) is False


def test_is_link_entry_is_true_on_a_broken_entry():
    class Broken:
        path = "x"

        def is_symlink(self):
            raise OSError("cannot inspect")

    assert fs.is_link_entry(Broken()) is True
