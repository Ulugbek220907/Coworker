"""Pull searchable text out of office documents.

Deliberately dependency-light. ``.docx``, ``.xlsx`` and ``.pptx`` are just ZIP
archives of XML, so the standard library handles them without installing
anything - which keeps the desktop download small. PDF is the one format that
genuinely needs a parser, and it degrades gracefully when PyMuPDF is absent.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from pathlib import Path

MAX_CHARS = 20_000  # more than enough to decide whether a file is the one

TEXT_EXT = {".txt", ".md", ".log", ".json", ".xml", ".html", ".htm", ".rtf", ".ini", ".yml", ".yaml"}
OFFICE_EXT = {".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm"}
SUPPORTED = TEXT_EXT | OFFICE_EXT | {".pdf", ".csv", ".tsv"}

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t ]+")
_NL = re.compile(r"\n{3,}")


def can_read(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED


def extract(path: str | Path, limit: int = MAX_CHARS) -> str:
    """Best-effort plain text. Returns "" rather than raising."""
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext in TEXT_EXT:
            return _clean(_read_text(p, limit))
        if ext in (".csv", ".tsv"):
            return _clean(_read_csv(p, limit))
        if ext in (".docx", ".docm"):
            return _clean(_read_docx(p, limit))
        if ext in (".xlsx", ".xlsm"):
            return _clean(_read_xlsx(p, limit))
        if ext in (".pptx", ".pptm"):
            return _clean(_read_pptx(p, limit))
        if ext == ".pdf":
            return _clean(_read_pdf(p, limit))
    except Exception:
        return ""
    return ""


# ------------------------------------------------------------------ readers

def _read_text(p: Path, limit: int) -> str:
    raw = p.read_bytes()[: limit * 4]
    text = _decode(raw)
    if p.suffix.lower() in (".html", ".htm", ".xml", ".rtf"):
        text = _TAG.sub(" ", text)
    return text[:limit]


def _read_csv(p: Path, limit: int) -> str:
    out: list[str] = []
    total = 0
    with p.open("r", encoding=_sniff_encoding(p), errors="replace", newline="") as fh:
        delim = "\t" if p.suffix.lower() == ".tsv" else ","
        for row in csv.reader(fh, delimiter=delim):
            line = " ".join(c.strip() for c in row if c.strip())
            if not line:
                continue
            out.append(line)
            total += len(line)
            if total > limit:
                break
    return "\n".join(out)


def _read_docx(p: Path, limit: int) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(p) as z:
        names = ["word/document.xml"]
        # Headers and footers often carry the counterparty name.
        names += sorted(
            n for n in z.namelist()
            if re.fullmatch(r"word/(header|footer)\d*\.xml", n)
        )
        for name in names:
            if name not in z.namelist():
                continue
            xml = _decode(z.read(name))
            # Paragraph and line breaks must survive as real newlines.
            xml = re.sub(r"</w:p>", "\n", xml)
            xml = re.sub(r"<w:br[^>]*/?>", "\n", xml)
            xml = re.sub(r"</w:tc>", "\t", xml)
            parts.append(_TAG.sub("", xml))
            if sum(len(x) for x in parts) > limit:
                break
    return _unescape("".join(parts))[:limit]


def _read_xlsx(p: Path, limit: int) -> str:
    """Walk the worksheets cell by cell.

    Reading only ``sharedStrings.xml`` is the obvious shortcut and it is
    wrong twice over: numbers never appear there at all, and writers such as
    openpyxl emit inline strings instead, producing a workbook with no shared
    string table whatsoever. Either way the interesting content - the amounts
    somebody is asking about - is invisible. So resolve each cell properly.
    """
    parts: list[str] = []
    with zipfile.ZipFile(p) as z:
        names = z.namelist()

        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            table = _decode(z.read("xl/sharedStrings.xml"))
            # One entry per <si>, with its runs flattened.
            shared = [
                _unescape(_TAG.sub("", si))
                for si in re.findall(r"<si>(.*?)</si>", table, re.S)
            ]

        if "xl/workbook.xml" in names:
            wb = _decode(z.read("xl/workbook.xml"))
            sheet_names = re.findall(r'<sheet[^>]*name="([^"]+)"', wb)
        else:
            sheet_names = []

        sheets = sorted(
            (n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
            key=lambda n: int(re.search(r"(\d+)", n).group(1)),
        )
        for i, name in enumerate(sheets):
            title = _unescape(sheet_names[i]) if i < len(sheet_names) else f"Sheet{i + 1}"
            parts.append(f"[{title}]")
            parts.append(_read_sheet_xml(_decode(z.read(name)), shared))
            if sum(len(x) for x in parts) > limit:
                break

    return _clean("\n".join(parts))[:limit]


_ROW = re.compile(r"<row\b[^>]*>(.*?)</row>", re.S)
_CELL = re.compile(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", re.S)
_VALUE = re.compile(r"<v>(.*?)</v>", re.S)


def _read_sheet_xml(xml: str, shared: list[str]) -> str:
    """One text line per spreadsheet row, cells separated by tabs."""
    lines: list[str] = []
    for body in _ROW.findall(xml):
        cells: list[str] = []
        for attrs, content in _CELL.findall(body):
            if not content:
                continue
            kind = (re.search(r'\bt="([^"]+)"', attrs) or [None, "n"])[1]

            if kind == "inlineStr":
                cells.append(_unescape(_TAG.sub("", content)).strip())
                continue

            raw = _VALUE.search(content)
            if not raw:
                continue
            text = _unescape(raw.group(1)).strip()

            if kind == "s":                       # index into the shared table
                try:
                    text = shared[int(text)]
                except (ValueError, IndexError):
                    pass
            elif kind == "b":
                text = "TRUE" if text == "1" else "FALSE"
            cells.append(text)

        line = "\t".join(c for c in cells if c)
        if line:
            lines.append(line)
    return "\n".join(lines)


def _read_pptx(p: Path, limit: int) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(p) as z:
        slides = sorted(
            n for n in z.namelist()
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)
        )
        for name in slides:
            xml = _decode(z.read(name))
            xml = re.sub(r"</a:p>", "\n", xml)
            parts.append(_TAG.sub("", xml))
            if sum(len(x) for x in parts) > limit:
                break
    return _unescape("\n".join(parts))[:limit]


def _read_pdf(p: Path, limit: int) -> str:
    try:
        import pymupdf as fitz      # PyMuPDF; `fitz` is the legacy alias
    except ImportError:
        try:
            import fitz
        except ImportError:
            return ""
    out: list[str] = []
    with fitz.open(p) as doc:
        for page in doc:
            out.append(page.get_text())
            if sum(len(x) for x in out) > limit:
                break
    return "\n".join(out)[:limit]


# ------------------------------------------------------------------ helpers

def _decode(raw: bytes) -> str:
    """Office XML is UTF-8; loose text files around here are often CP1251."""
    for enc in ("utf-8", "utf-8-sig", "cp1251", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _sniff_encoding(p: Path) -> str:
    head = p.read_bytes()[:4096]
    try:
        head.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1251"


def _unescape(text: str) -> str:
    return (
        text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        .replace("&quot;", '"').replace("&apos;", "'").replace("&#39;", "'")
    )


def _clean(text: str) -> str:
    text = _WS.sub(" ", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _NL.sub("\n\n", text).strip()
