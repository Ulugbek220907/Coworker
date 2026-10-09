"""Pull searchable text out of office documents.

Deliberately dependency-light. ``.docx``, ``.xlsx`` and ``.pptx`` are just ZIP
archives of XML, so the standard library handles them without installing
anything - which keeps the desktop download small. PDF is the one format that
genuinely needs a parser, and it degrades gracefully when PyMuPDF is absent.

Three limits protect the machine. An archive member that would expand past
``MAX_ZIP_MEMBER_BYTES`` refuses the whole file (a zip bomb is a small file with
one enormous member), and all members of one archive share ``MAX_ARCHIVE_BYTES``.
Text files are read in chunks up to a byte cap. Every scan here is linear in the
text, so a hostile part cannot stall the caller.
"""
from __future__ import annotations

import codecs
import csv
import io
import logging
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

log = logging.getLogger("extract")

MAX_CHARS = 20_000  # more than enough to decide whether a file is the one
MAX_ZIP_MEMBER_BYTES = 20 * 1024 * 1024  # one archive member, uncompressed
MAX_ARCHIVE_BYTES = 2 * MAX_ZIP_MEMBER_BYTES  # every member read from one archive, uncompressed
_CHUNK = 64 * 1024

TEXT_EXT = {".txt", ".md", ".log", ".json", ".xml", ".html", ".htm", ".rtf", ".ini", ".yml", ".yaml"}
OFFICE_EXT = {".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm"}
SUPPORTED = TEXT_EXT | OFFICE_EXT | {".pdf", ".csv", ".tsv"}

# A tag never contains '<', so a stray '<' ends a match at once. The old
# ``<[^>]+>`` ran on to the end of the text looking for '>' and was quadratic.
_TAG = re.compile(r"<[^<>]*>")
# Spaces, no-break spaces and tabs inside a line. Tabs are kept apart from
# spaces because they separate table cells; a run of either is one character.
_SPACES = re.compile(r"[    ]+")
_TABS = re.compile(r"\t+")
_NL = re.compile(r"\n{3,}")

_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


class ZipRefused(ValueError):
    """An archive member expands past MAX_ZIP_MEMBER_BYTES. The whole file is refused."""


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
    except ZipRefused as exc:
        log.warning("refused %s: %s", p.name, exc)
        return ""
    except Exception:
        return ""
    return ""


# ------------------------------------------------------------------ readers

def _read_text(p: Path, limit: int) -> str:
    text = _decode_file(p, limit * 4)
    if p.suffix.lower() in (".html", ".htm", ".xml", ".rtf"):
        text = _TAG.sub(" ", text)
    return text[:limit]


def _read_csv(p: Path, limit: int) -> str:
    text = _decode_file(p, limit * 4)
    delim = "\t" if p.suffix.lower() == ".tsv" else ","
    out: list[str] = []
    total = 0
    for row in csv.reader(io.StringIO(text, newline=""), delimiter=delim):
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
    with _Archive(p) as z:
        present = z.names()
        names = ["word/document.xml"]
        # Headers and footers often carry the counterparty name.
        names += sorted(n for n in present if re.fullmatch(r"word/(header|footer)\d*\.xml", n))
        for name in names:
            if name not in present:
                continue
            xml = _decode(z.member(name))
            # Paragraph and line breaks must survive as real newlines.
            xml = re.sub(r"</w:p>", "\n", xml)
            xml = re.sub(r"<w:br[^<>]*>", "\n", xml)
            xml = re.sub(r"</w:tc>", "\t", xml)
            parts.append(_TAG.sub("", xml))
            if sum(len(x) for x in parts) > limit:
                break
    return _unescape("".join(parts))[:limit]


def _read_xlsx(p: Path, limit: int) -> str:
    """Walk the worksheets cell by cell, in workbook tab order.

    Reading only ``sharedStrings.xml`` is the obvious shortcut and it is
    wrong twice over: numbers never appear there at all, and writers such as
    openpyxl emit inline strings instead, producing a workbook with no shared
    string table whatsoever. So each cell is resolved properly.

    Tab order is not file order. Once a sheet is moved, ``sheet2.xml`` can be the
    first tab, so titles come from ``workbook.xml`` and each tab's part is found
    through ``workbook.xml.rels``.
    """
    parts: list[str] = []
    with _Archive(p) as z:
        names = z.names()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            shared = _shared_strings(_decode(z.member("xl/sharedStrings.xml")))
        for title, member in _xlsx_sheets(z, names):
            parts.append(f"[{title}]")
            parts.append(_read_sheet_xml(_decode(z.member(member)), shared))
            if sum(len(x) for x in parts) > limit:
                break
    return _clean("\n".join(parts))[:limit]


def _xlsx_sheets(z: "_Archive", names: set[str]) -> list[tuple[str, str]]:
    """(title, member name) for each worksheet, in tab order."""
    by_number = sorted(
        (n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
        key=_part_number,
    )
    fallback = [(f"Sheet{i + 1}", n) for i, n in enumerate(by_number)]
    if "xl/workbook.xml" not in names:
        return fallback
    try:
        workbook = ET.fromstring(z.member("xl/workbook.xml"))
        relations = _relationships(z, names)
    except ET.ParseError:
        return fallback

    out: list[tuple[str, str]] = []
    for el in workbook.iter():
        if _local(el.tag) != "sheet":
            continue
        rel_id = next((v for k, v in el.attrib.items() if _local(k) == "id"), "")
        target, rel_type = relations.get(rel_id, ("", ""))
        if not target or (rel_type and not rel_type.endswith("/worksheet")):
            continue
        member = target[1:] if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        if member in names:
            out.append((el.get("name") or f"Sheet{len(out) + 1}", member))
    return out or fallback


def _relationships(z: "_Archive", names: set[str]) -> dict[str, tuple[str, str]]:
    """Relationship id -> (target, type) for the workbook."""
    if "xl/_rels/workbook.xml.rels" not in names:
        return {}
    root = ET.fromstring(z.member("xl/_rels/workbook.xml.rels"))
    out: dict[str, tuple[str, str]] = {}
    for el in root.iter():
        if _local(el.tag) == "Relationship" and el.get("Id"):
            out[el.get("Id")] = (el.get("Target") or "", el.get("Type") or "")
    return out


def _elements(xml: str, tag: str) -> list[tuple[str, str]]:
    """(attributes, inner XML) of each closed ``<tag>`` element, in document order.

    One forward pass; a lazy ``.*?`` would rescan the text for each unclosed opener.
    """
    opener = re.compile(rf"<{tag}\b([^<>]*)(?<!/)>")
    closer = f"</{tag}>"
    found: list[tuple[str, str]] = []
    pos = 0
    while (match := opener.search(xml, pos)) is not None:
        end = xml.find(closer, match.end())
        if end < 0:
            break  # no later closer either: the rest is not a complete element
        found.append((match.group(1), xml[match.end():end]))
        pos = end + len(closer)
    return found


def _shared_strings(xml: str) -> list[str]:
    # One entry per <si>, with its runs flattened. <si> may carry attributes.
    return [_unescape(_TAG.sub("", inner)) for _, inner in _elements(xml, "si")]


_CELL_TYPE = re.compile(r'\bt="([^"]+)"')


def _read_sheet_xml(xml: str, shared: list[str]) -> str:
    """One text line per spreadsheet row, cells separated by tabs."""
    lines: list[str] = []
    for _, body in _elements(xml, "row"):
        cells: list[str] = []
        for attrs, content in _elements(body, "c"):
            if not content:
                continue
            match = _CELL_TYPE.search(attrs)
            kind = match.group(1) if match else "n"

            if kind == "inlineStr":
                cells.append(_unescape(_TAG.sub("", content)).strip())
                continue

            values = _elements(content, "v")
            if not values:
                continue
            text = _unescape(values[0][1]).strip()

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
    with _Archive(p) as z:
        slides = sorted(
            (n for n in z.names() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=_part_number,
        )
        for name in slides:
            xml = _decode(z.member(name))
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

class _Archive:
    """One ZIP file, read member by member under one expansion budget.

    The budget counts bytes actually produced, so a header that understates a
    member cannot slip past it, and a bomb of many members is bounded as a whole.
    """

    def __init__(self, path: str | Path) -> None:
        self._zip = zipfile.ZipFile(path)
        self._left = MAX_ARCHIVE_BYTES

    def __enter__(self) -> "_Archive":
        return self

    def __exit__(self, *exc: object) -> None:
        self._zip.close()

    def names(self) -> set[str]:
        return set(self._zip.namelist())

    def member(self, name: str) -> bytes:
        data = _member(self._zip, name)
        self._left -= len(data)
        if self._left < 0:
            raise ZipRefused(f"the archive expands past {MAX_ARCHIVE_BYTES} bytes")
        return data


def _member(z: zipfile.ZipFile, name: str) -> bytes:
    """One archive member, refused when it would expand past the limit.

    The header size is checked first, then the bytes actually produced. A header
    can understate the size, so the read itself is capped.
    """
    info = z.getinfo(name)
    if info.file_size > MAX_ZIP_MEMBER_BYTES:
        raise ZipRefused(f"{name} would expand to {info.file_size} bytes")
    with z.open(info) as fh:
        data = fh.read(MAX_ZIP_MEMBER_BYTES + 1)
    if len(data) > MAX_ZIP_MEMBER_BYTES:
        raise ZipRefused(f"{name} expands past {MAX_ZIP_MEMBER_BYTES} bytes")
    return data


def _decode_file(p: Path, max_bytes: int) -> str:
    """Decode at most ``max_bytes`` of ``p``, reading it in chunks.

    A byte-order mark chooses the codec and is removed. Without one, UTF-8 is
    tried first, then the Cyrillic code pages. A character cut in half by the
    byte cap is dropped, which used to push a valid UTF-8 file into the wrong
    code page.
    """
    with p.open("rb") as fh:
        head = fh.read(4)
    candidates = [enc for bom, enc in _BOMS if head.startswith(bom)]
    candidates += ["utf-8", "cp1251", "cp1252"]
    for enc in candidates:
        try:
            return _decode_chunks(p, enc, max_bytes)
        except UnicodeDecodeError:
            continue
    return _decode_chunks(p, "utf-8", max_bytes, errors="replace")


def _decode_chunks(p: Path, encoding: str, max_bytes: int, errors: str = "strict") -> str:
    decoder = codecs.getincrementaldecoder(encoding)(errors=errors)
    parts: list[str] = []
    left = max_bytes
    at_end = False
    with p.open("rb") as fh:
        while left > 0:
            chunk = fh.read(min(_CHUNK, left))
            if not chunk:
                at_end = True
                break
            left -= len(chunk)
            parts.append(decoder.decode(chunk))
        # Only at the real end of the file may an incomplete sequence be an error.
        parts.append(decoder.decode(b"", final=at_end))
    return "".join(parts)


def _decode(raw: bytes) -> str:
    """Decode an in-memory member. Office XML is UTF-8; loose text is often CP1251."""
    for bom, enc in _BOMS:
        if raw.startswith(bom):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                break
    for enc in ("utf-8", "cp1251", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _part_number(name: str) -> int:
    """The number at the end of a part name, so slide10 sorts after slide2."""
    match = re.search(r"(\d+)\.xml$", name)
    return int(match.group(1)) if match else 0


def _local(tag: str) -> str:
    """Tag or attribute name without its namespace."""
    return tag.rsplit("}", 1)[-1]


def _unescape(text: str) -> str:
    return (
        text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        .replace("&quot;", '"').replace("&apos;", "'").replace("&#39;", "'")
    )


def _clean(text: str) -> str:
    """Normalise line endings and spacing. Runs of spaces become one space, runs of tabs one tab."""
    text = _SPACES.sub(" ", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = _TABS.sub("\t", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _NL.sub("\n\n", text).strip()
