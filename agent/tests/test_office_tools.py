"""Office tool surface: the five SPECS, their argument schemas and checks, and result mapping.

Path refusals are checked against the real policy/paths.py rule, and the
argument checks are also run through the real PolicyKernel, so the tests see
the same decision the dispatcher would.
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import openpyxl
import pymupdf
import pytest

import coworker.config
from coworker import office
from coworker.core.types import Autonomy, CallContext, Decision, Provenance, Tier, Verdict
from coworker.policy.kernel import PolicyKernel
from coworker.policy.paths import check_path
from coworker.tools import office as tools
from coworker.tools.registry import Registry, Services, ToolCall, validate_args

SPEC = {spec.name: spec for spec in tools.SPECS}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("COWORKER_HOME", str(root))
    monkeypatch.setattr(coworker.config, "config_dir", lambda: root)
    return root


@pytest.fixture
def book(tmp_path) -> Path:
    path = tmp_path / "Book.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "Budget"
    wb.active["A2"], wb.active["B2"] = "Rent", 1200
    wb.create_sheet("Бюджет")["A1"] = "Qator"
    wb.save(path)
    return path


def _ctx() -> CallContext:
    return CallContext(
        turn_id="t1", actor="owner", chat_id=1, autonomy=Autonomy.ASK_FOR_WRITES,
        grants=frozenset({"office"}), generation=0, provenance=Provenance.OWNER,
    )


def _run(name: str, args: dict):
    return SPEC[name].handler(ToolCall(name=name, args=args, ctx=_ctx(), svc=Services()))


def _write_check(args: dict) -> Verdict | None:
    (hook,) = SPEC["sheet_write"].arg_checks
    return hook(args, _ctx(), None)


# ------------------------------------------------------------- the surface

def test_exactly_the_five_office_tools_exist():
    assert [s.name for s in tools.SPECS] == ["sheet_list", "sheet_read", "sheet_write", "to_pdf", "pdf_pages"]


@pytest.mark.parametrize("name, tier, path_write, surfaced, untrusted", [
    ("sheet_list", Tier.READ, False, (), True),
    ("sheet_read", Tier.READ, False, (), True),
    ("sheet_write", Tier.LOCAL_WRITE, True, ("path",), False),
    ("to_pdf", Tier.LOCAL_WRITE, False, (), False),
    ("pdf_pages", Tier.LOCAL_WRITE, False, (), False),
])
def test_each_spec_matches_the_contract_table(name, tier, path_write, surfaced, untrusted):
    spec = SPEC[name]
    assert spec.family == "office"
    assert spec.gov_class == "OFFICE"
    assert spec.tier == tier
    assert spec.path_args == ("path",)
    assert spec.path_write is path_write
    assert spec.requires_surfaced == surfaced
    assert spec.untrusted is untrusted


def test_registry_accepts_every_spec_and_shows_them_to_the_office_grant():
    reg = Registry()
    reg.register_many(tools.SPECS)
    assert reg.visible_names(frozenset({"office"})) == set(SPEC)
    assert reg.visible_names(frozenset({"files"})) == set()


@pytest.mark.parametrize("name, args, error", [
    ("sheet_list", {}, "missing argument: path"),
    ("sheet_list", {"path": "a.xlsx", "extra": 1}, "unexpected argument: extra"),
    ("sheet_read", {"path": "a.xlsx", "sheet": None}, "sheet must not be null"),
    ("sheet_write", {"path": "a.xlsx"}, "missing argument: changes"),
    ("sheet_write", {"path": "a.xlsx", "changes": "B5=1"}, "changes must be an object"),
    ("pdf_pages", {"path": "a.pdf"}, "missing argument: pages"),
    ("pdf_pages", {"path": "a.pdf", "pages": "1" * 101}, "pages is too long"),
    ("to_pdf", {"path": 7}, "path must be a string"),
])
def test_argument_schemas_reject_malformed_calls(name, args, error):
    assert validate_args(SPEC[name].parameters, args) == error


def test_a_well_formed_write_passes_the_schema():
    assert validate_args(SPEC["sheet_write"].parameters, {"path": "a.xlsx", "changes": {"B5": 1}}) is None


# ----------------------------------------------------- sheet_write arg check

CASES = [
    # changes, sheet, file name, expected reason fragment (None = allowed)
    ({"B5": 500000}, None, "Book.xlsx", None),
    ({"b5": "12"}, "budget", "Book.xlsx", None),                       # lower-case ref, loose sheet name
    ({"C3": None}, "Бюджет", "Book.xlsx", None),                       # None clears a cell
    ({f"A{i}": i for i in range(1, 51)}, None, "Book.xlsx", None),     # exactly 50 cells
    ({f"A{i}": i for i in range(1, 52)}, None, "Book.xlsx", "50 tadan"),
    ({"XFD1": 1}, None, "Book.xlsx", None),                            # last column, inside the grid
    ({}, None, "Book.xlsx", "O'zgarishlar"),
    ({"A0": 1}, None, "Book.xlsx", "Noto'g'ri katak"),
    ({"B5:C6": 1}, None, "Book.xlsx", "Noto'g'ri katak"),
    ({"Budget!A1": 1}, None, "Book.xlsx", "Noto'g'ri katak"),
    ({"XFE1": 1}, None, "Book.xlsx", "Noto'g'ri katak"),               # column 16385
    ({"A1048577": 1}, None, "Book.xlsx", "Noto'g'ri katak"),           # row past the grid
    ({"A1": [1]}, None, "Book.xlsx", "qiymat"),
    ({"A1": {"x": 1}}, None, "Book.xlsx", "qiymat"),
    ({"A1": float("nan")}, None, "Book.xlsx", "cheksiz"),
    ({"A1": float("inf")}, None, "Book.xlsx", "cheksiz"),
    ({"A1": "x" * 32768}, None, "Book.xlsx", "juda uzun"),
    ({"b5": 1, "B5": 2}, None, "Book.xlsx", "ikki marta"),
    ({"B5": 1}, "Nope", "Book.xlsx", "varag'i yo'q"),
    ({"B5": 1}, None, "Book.xlsm", "Makrosli"),
    ({"B5": 1}, None, "Book.xls", "Makrosli"),
    ({"B5": 1}, None, "Template.xltx", "Faqat .xlsx"),
    ({"B5": 1}, None, "Missing.xlsx", "Fayl yo'q"),
]


@pytest.mark.parametrize("changes, sheet, file_name, expect", CASES)
def test_sheet_write_arg_check_table(tmp_path, book, changes, sheet, file_name, expect):
    target = book if file_name == "Book.xlsx" else tmp_path / file_name
    verdict = _write_check({"path": str(target), "changes": changes, "sheet": sheet})

    if expect is None:
        assert verdict is None
    else:
        assert verdict is not None
        assert verdict.decision == Decision.DENY
        assert verdict.code == "arg_invalid"
        assert expect in verdict.reason


def test_a_write_that_fails_its_check_is_refused_before_any_card(book):
    verdict = _write_check({"path": str(book), "changes": {"A0": 1}})
    assert verdict.decision == Decision.DENY


def test_the_arg_check_never_writes(book):
    before = book.read_bytes()
    _write_check({"path": str(book), "changes": {"B5": 1}, "sheet": "Budget"})
    assert book.read_bytes() == before


def test_summary_names_the_file_and_the_cells_in_owner_language():
    text = SPEC["sheet_write"].summary({
        "path": "C:/x/Budget.xlsx", "changes": {"B5": 1, "C5": 2}, "sheet": "Budget",
    })
    assert "Budget.xlsx" in text
    assert "2 ta katak" in text
    assert "B5" in text and "C5" in text
    assert len(text) <= 600


# ----------------------------------------------------------- path rule

@pytest.mark.parametrize("name", ["sheet_list", "sheet_read", "sheet_write", "to_pdf", "pdf_pages"])
def test_traversal_is_refused_by_check_path_for_every_office_path(name):
    spec = SPEC[name]
    for _ in spec.path_args:
        assert check_path("..\\..\\secrets\\Report.pdf", write=spec.path_write) == "path_invalid"


def test_kernel_refuses_pdf_pages_traversal_at_the_path_step():
    verdict = PolicyKernel().evaluate(
        SPEC["pdf_pages"], {"path": "..\\..\\secrets\\Report.pdf", "pages": "1"}, _ctx(),
    )
    assert verdict.decision == Decision.DENY
    assert verdict.code == "path_invalid"


def test_kernel_refuses_a_bad_change_set_before_any_confirmation_card(book):
    surfaced = frozenset({os.path.normcase(os.path.normpath(str(book)))})
    ctx = replace(_ctx(), surfaced=surfaced)
    verdict = PolicyKernel().evaluate(
        SPEC["sheet_write"], {"path": str(book), "changes": {"A0": 1}, "sheet": "Budget"}, ctx,
    )
    assert verdict.decision == Decision.DENY
    assert verdict.code == "arg_invalid"


def test_kernel_asks_for_a_valid_write_with_a_summary(book):
    surfaced = frozenset({os.path.normcase(os.path.normpath(str(book)))})
    ctx = replace(_ctx(), surfaced=surfaced)
    verdict = PolicyKernel().evaluate(
        SPEC["sheet_write"], {"path": str(book), "changes": {"B2": 1300}, "sheet": "Budget"}, ctx,
    )
    assert verdict.decision == Decision.CONFIRM
    assert "Book.xlsx" in verdict.summary


def test_pdf_pages_traversal_in_the_page_list_writes_nothing_outside_scratch(tmp_path, home):
    src = tmp_path / "Report.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(src)
    doc.close()
    before = {p.name for p in tmp_path.iterdir()}

    result = _run("pdf_pages", {"path": str(src), "pages": "..\\..\\escape"})

    assert not result.ok and result.code == "arg_invalid"
    assert {p.name for p in tmp_path.iterdir()} == before


# ------------------------------------------------------------ result mapping

def test_sheet_read_result_is_untrusted_and_sends_nothing(book):
    result = _run("sheet_read", {"path": str(book), "sheet": "Budget"})
    assert result.ok and result.untrusted is True
    assert result.sendable == ()
    assert "Rent" in result.data["table"]


def test_sheet_list_result_is_untrusted(book):
    result = _run("sheet_list", {"path": str(book)})
    assert result.ok and result.untrusted is True
    assert result.sendable == ()


def test_a_non_excel_read_maps_to_arg_invalid(tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("hi")
    result = _run("sheet_list", {"path": str(notes)})
    assert not result.ok and result.code == "arg_invalid"


def test_sheet_write_result_reports_applied_cells_and_the_backup(book):
    result = _run("sheet_write", {"path": str(book), "changes": {"B2": 1300}, "sheet": "Budget"})

    assert result.ok and result.untrusted is False
    assert result.sendable == ()
    assert result.data["applied"] == {"B2": 1300}
    assert Path(result.data["backup"]).exists()


def test_sheet_write_on_a_missing_file_fails_cleanly(tmp_path):
    result = _run("sheet_write", {"path": str(tmp_path / "gone.xlsx"), "changes": {"A1": 1}})
    assert not result.ok and result.code == "arg_invalid"


def test_sheet_write_refused_macro_file_is_arg_invalid(tmp_path):
    target = tmp_path / "Book.xlsm"
    target.write_bytes(b"x")
    result = _run("sheet_write", {"path": str(target), "changes": {"A1": 1}})
    assert not result.ok and result.code == "arg_invalid"
    assert target.read_bytes() == b"x"


def test_pdf_pages_result_is_the_sendable_file_and_it_lives_in_scratch(tmp_path, home):
    src = tmp_path / "Report.pdf"
    doc = pymupdf.open()
    for i in range(3):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    doc.save(src)
    doc.close()

    result = _run("pdf_pages", {"path": str(src), "pages": "2"})

    assert result.ok
    assert result.sendable == (result.data["path"],)
    assert Path(result.sendable[0]).parent.parent == home / "scratch"


def test_to_pdf_result_is_the_sendable_file(tmp_path, monkeypatch):
    src = tmp_path / "memo.docx"
    src.write_bytes(b"docx")

    def fake_com(source, target):
        target.write_bytes(b"%PDF fake")
        return {"ok": True, "path": str(target), "via": "Office/Word"}

    monkeypatch.setattr(office, "com_apps", lambda: ["Word"])
    monkeypatch.setattr(office, "_pdf_via_com", fake_com)

    result = _run("to_pdf", {"path": str(src)})

    assert result.ok
    assert result.sendable == (result.data["path"],)


def test_to_pdf_without_any_backend_says_so_in_owner_language(tmp_path, monkeypatch):
    src = tmp_path / "memo.docx"
    src.write_bytes(b"docx")
    monkeypatch.setattr(office, "com_apps", lambda: [])
    monkeypatch.setattr(office, "libreoffice_path", lambda: "")

    result = _run("to_pdf", {"path": str(src)})

    assert not result.ok
    assert result.code == "not_configured"
    assert "LibreOffice" in result.error
    assert result.sendable == ()
