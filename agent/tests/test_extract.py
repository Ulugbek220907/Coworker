"""Text extraction from office files, PDFs and text files, on generated fixtures.

The OOXML files are written by hand with ``zipfile``, so the tests control the
exact part names, relationships and tab order that the reader has to handle.
"""
from __future__ import annotations

import time
import zipfile
from pathlib import Path

import pymupdf
import pytest

from coworker import extract as ex

MB = 1024 * 1024


# ----------------------------------------------------------------- fixtures

def _write_zip(path: Path, parts: dict[str, bytes | str]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return path


def _docx(path: Path, paragraphs: list[str]) -> Path:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="urn:w"><w:body>{body}</w:body></w:document>'
    return _write_zip(path, {"word/document.xml": xml})


def _xlsx(path: Path, tabs: list[tuple[str, str, str]], rows: dict[str, str],
          shared: list[str] | None = None) -> Path:
    """``tabs`` are (title, relationship id, worksheet part) in tab order.

    ``rows`` maps each worksheet part to its ``<row>`` XML. The relationship
    target is the part name relative to ``xl/``, as Excel writes it.
    """
    tab_xml = "".join(
        f'<sheet name="{title}" sheetId="{i + 1}" r:id="{rid}"/>' for i, (title, rid, _) in enumerate(tabs)
    )
    workbook = (
        '<?xml version="1.0"?><workbook xmlns="urn:main" xmlns:r="urn:rel">'
        f"<sheets>{tab_xml}</sheets></workbook>"
    )
    rels = "".join(
        f'<Relationship Id="{rid}" Type="http://x/worksheet" Target="{part[len("xl/"):]}"/>'
        for _, rid, part in tabs
    )
    parts: dict[str, bytes | str] = {
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": f'<?xml version="1.0"?><Relationships xmlns="urn:pkg">{rels}</Relationships>',
    }
    for part, body in rows.items():
        parts[part] = f'<?xml version="1.0"?><worksheet xmlns="urn:main"><sheetData>{body}</sheetData></worksheet>'
    if shared is not None:
        items = "".join(f"<si><t>{s}</t></si>" for s in shared)
        parts["xl/sharedStrings.xml"] = f'<?xml version="1.0"?><sst xmlns="urn:main">{items}</sst>'
    return _write_zip(path, parts)


def _pptx(path: Path, slides: dict[int, str]) -> Path:
    parts = {
        f"ppt/slides/slide{n}.xml": f"<p:sld xmlns:a='urn:a'><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:sld>"
        for n, text in slides.items()
    }
    return _write_zip(path, parts)


def _pdf(path: Path, text: str) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()
    return path


# ------------------------------------------------------------ office formats

def test_docx_paragraphs_come_out_on_their_own_lines(tmp_path):
    path = _docx(tmp_path / "contract.docx", ["Shartnoma raqami 17", "Tekstil bo'yicha"])
    text = ex.extract(path)
    assert text.splitlines() == ["Shartnoma raqami 17", "Tekstil bo'yicha"]


def test_pdf_text_comes_from_pymupdf(tmp_path):
    path = _pdf(tmp_path / "Ma.pdf", "Shartnoma 2024 tekstil")
    assert "Shartnoma 2024" in ex.extract(path)


def test_xlsx_reads_numbers_shared_strings_and_inline_strings(tmp_path):
    rows = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>1200</v></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>Ijara</t></is></c><c r="B2" t="b"><v>1</v></c></row>'
    )
    path = _xlsx(tmp_path / "budget.xlsx", [("Budget", "rId1", "xl/worksheets/sheet1.xml")],
                 {"xl/worksheets/sheet1.xml": rows}, shared=["Rent"])
    text = ex.extract(path)
    assert "Rent\t1200" in text
    assert "Ijara\tTRUE" in text


def test_xlsx_titles_follow_tab_order_after_sheets_are_moved(tmp_path):
    # The first tab is "Zeta", which is wired to sheet2.xml. Titles come from the
    # tab order and each tab's data from its relationship, not from file numbers.
    rows = {
        "xl/worksheets/sheet1.xml": '<row r="1"><c r="A1" t="inlineStr"><is><t>Alpha data</t></is></c></row>',
        "xl/worksheets/sheet2.xml": '<row r="1"><c r="A1" t="inlineStr"><is><t>Zeta data</t></is></c></row>',
    }
    path = _xlsx(
        tmp_path / "moved.xlsx",
        [("Zeta", "rId2", "xl/worksheets/sheet2.xml"), ("Alpha", "rId1", "xl/worksheets/sheet1.xml")],
        rows,
    )
    text = ex.extract(path)
    zeta, alpha = text.index("[Zeta]"), text.index("[Alpha]")
    assert zeta < alpha
    assert text.index("Zeta data") > zeta
    assert text.index("Zeta data") < alpha
    assert text.index("Alpha data") > alpha


def test_pptx_slides_come_out_in_numeric_order(tmp_path):
    path = _pptx(tmp_path / "deck.pptx", {10: "ten", 2: "two", 1: "one"})
    text = ex.extract(path)
    assert text.index("one") < text.index("two") < text.index("ten")


# ------------------------------------------------------------ zip limits

def test_zip_member_limit_is_twenty_megabytes():
    assert ex.MAX_ZIP_MEMBER_BYTES == 20 * MB


def test_zip_bomb_is_refused_as_a_whole(tmp_path):
    bomb = _docx(tmp_path / "bomb.docx", ["a" * (25 * MB)])
    assert bomb.stat().st_size < MB  # a small archive that expands far past the limit
    assert ex.extract(bomb) == ""
    with zipfile.ZipFile(bomb) as z:
        with pytest.raises(ex.ZipRefused):
            ex._member(z, "word/document.xml")


def test_member_under_the_limit_is_read(tmp_path):
    path = _docx(tmp_path / "big.docx", ["b" * (2 * MB)])
    text = ex.extract(path, limit=3 * MB)
    assert text.startswith("bbbb") and len(text) > 2 * MB - 10


def test_member_over_the_limit_is_refused_even_at_a_small_limit(tmp_path, monkeypatch):
    path = _docx(tmp_path / "small_limit.docx", ["c" * 100])
    monkeypatch.setattr(ex, "MAX_ZIP_MEMBER_BYTES", 50)
    assert ex.extract(path) == ""


# ------------------------------------------------------------ encodings

def test_utf8_bom_is_removed_from_text(tmp_path):
    path = tmp_path / "note.txt"
    path.write_bytes(b"\xef\xbb\xbf" + "Шартнома".encode("utf-8"))
    assert ex.extract(path) == "Шартнома"


def test_utf8_bom_is_removed_from_csv_first_cell(tmp_path):
    path = tmp_path / "rows.csv"
    path.write_bytes(b"\xef\xbb\xbfa,b\n1,2\n")
    assert ex.extract(path) == "a b\n1 2"


def test_utf16_bom_selects_the_codec(tmp_path):
    path = tmp_path / "wide.txt"
    path.write_bytes("Договор".encode("utf-16"))
    assert ex.extract(path) == "Договор"


def test_cp1251_is_used_when_utf8_fails(tmp_path):
    path = tmp_path / "old.txt"
    path.write_bytes("Договор на поставку".encode("cp1251"))
    assert ex.extract(path) == "Договор на поставку"


def test_character_cut_by_the_byte_cap_does_not_force_another_encoding(tmp_path):
    # "a" is one byte and each Cyrillic letter two, so byte 40 falls inside a letter.
    path = tmp_path / "cut.txt"
    path.write_bytes(("a" + "Ш" * 30).encode("utf-8"))
    assert ex._decode_file(path, max_bytes=40) == "a" + "Ш" * 19
    assert ex.extract(path, limit=10) == "a" + "Ш" * 9


def test_text_is_read_in_chunks_not_as_one_block(tmp_path, monkeypatch):
    path = tmp_path / "large.txt"
    path.write_text("line of text\n" * 300_000, encoding="utf-8")  # about 4 MB

    def forbidden(self):
        raise AssertionError("the whole file must not be read at once")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    text = ex.extract(path, limit=1000)
    assert text.startswith("line of text") and len(text) <= 1000


# ------------------------------------------------------------ whitespace

def test_runs_of_tabs_collapse_to_one_tab_and_spaces_to_one_space():
    assert ex._clean("A\t\t\tB   C D") == "A\tB C D"


def test_tabs_are_kept_as_cell_separators():
    assert ex._clean("one\ttwo") == "one\ttwo"


def test_blank_lines_are_capped_at_one():
    assert ex._clean("a\n\n\n\nb") == "a\n\nb"


def test_unsupported_extension_returns_empty(tmp_path):
    path = tmp_path / "image.bin"
    path.write_bytes(b"\x00\x01")
    assert ex.extract(path) == ""
    assert ex.can_read("x.DOCX") and not ex.can_read("x.bin")


# ------------------------------------------------ stray markup and expansion

def _timed(fn):
    started = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - started


def test_bare_less_than_signs_with_no_closing_bracket_are_read_in_linear_time(tmp_path):
    # Before the fix, each '<' scanned to the end of the member looking for '>':
    # 150 000 of them took several seconds, and a 2 MB member would take hours.
    member = "<" * 150_000 + "x"
    path = _write_zip(tmp_path / "stray.docx", {"word/document.xml": member})
    text, elapsed = _timed(lambda: ex.extract(path, limit=200_000))
    assert text == member
    assert elapsed < 2.0


def test_html_with_bare_less_than_signs_is_read_in_linear_time(tmp_path):
    path = tmp_path / "stray.html"
    path.write_text("<" * 100_000, encoding="utf-8")
    text, elapsed = _timed(lambda: ex.extract(path, limit=200_000))
    assert text == "<" * 100_000
    assert elapsed < 2.0


def test_tags_are_still_stripped_next_to_a_stray_less_than_sign(tmp_path):
    path = tmp_path / "page.html"
    path.write_text("<p>a < b <b>bold</b></p>", encoding="utf-8")
    assert ex.extract(path) == "a < b bold"


def test_worksheet_rows_that_never_close_do_not_stall_the_reader(tmp_path):
    rows = '<row r="1"><c r="A1" t="inlineStr"><is><t>kept</t></is></c></row>' + "<row>" * 20_000
    path = _xlsx(tmp_path / "unclosed.xlsx", [("Sheet", "rId1", "xl/worksheets/sheet1.xml")],
                 {"xl/worksheets/sheet1.xml": rows})
    text, elapsed = _timed(lambda: ex.extract(path))
    assert "kept" in text
    assert elapsed < 2.0


def test_shared_strings_that_never_close_do_not_stall_the_reader(tmp_path):
    path = _xlsx(tmp_path / "shared.xlsx", [("Sheet", "rId1", "xl/worksheets/sheet1.xml")],
                 {"xl/worksheets/sheet1.xml": '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'})
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("xl/sharedStrings.xml", '<sst><si><t>Rent</t></si>' + "<si>" * 20_000)
    text, elapsed = _timed(lambda: ex.extract(path))
    assert "Rent" in text
    assert elapsed < 2.0


def test_a_zip_with_many_members_shares_one_expansion_budget(tmp_path, monkeypatch):
    # Each member is under the per-member limit, but together they pass the archive budget.
    monkeypatch.setattr(ex, "MAX_ARCHIVE_BYTES", 1_000)
    parts = {"word/document.xml": "<w:p>" + "a" * 600 + "</w:p>"}
    for n in range(1, 4):
        parts[f"word/header{n}.xml"] = "<w:p>" + "h" * 600 + "</w:p>"
    path = _write_zip(tmp_path / "many.docx", parts)
    assert ex.extract(path) == ""


def test_the_archive_budget_holds_at_least_one_full_size_member():
    assert ex.MAX_ARCHIVE_BYTES >= ex.MAX_ZIP_MEMBER_BYTES
