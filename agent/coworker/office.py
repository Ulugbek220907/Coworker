"""Document operations that need no mouse, no screenshot and no focus.

This is the layer Microsoft's own UFO research calls "native API preferred,
GUI fallback", and it is deliberately built first: it is deterministic,
testable, works while the screen is locked, and covers most of what an office
assistant is actually asked to do.

Two backends, chosen per operation:

  pure Python (openpyxl / PyMuPDF)
      Always available, works on a machine with no Office installed, and is
      what gets exercised by the tests. Reading, cell edits and page surgery
      live here.

  COM (pywin32 + a real Office install), then LibreOffice
      Only reached for things pure Python genuinely cannot do faithfully:
      rendering a document to PDF with correct layout. If neither exists the
      caller is told plainly rather than handed a bad conversion.

Rules that hold for every operation here:

  * Anything that modifies a file takes a backup first, under a name that is
    never reused. A remote agent editing a contract with no undo is not
    something to be relaxed about.
  * A workbook is saved to a temporary file beside the original, flushed to
    disk and swapped in with one rename, so a failure at any step leaves the
    original untouched.
  * Generated files are written only under the scratch folder and published
    only once complete, so a half-made PDF is never reused as if it were done.
  * COM calls are bounded: an overrun is abandoned and counted, not waited on.
    LibreOffice runs with a scrubbed environment and is killed as a whole tree.
"""
from __future__ import annotations

import hashlib
import io
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .textutil import normalize

log = logging.getLogger("office")

MAX_ROWS = 40
MAX_COLS = 15
MAX_CHANGES = 50                  # cells per sheet_write call
MAX_CELL_CHARS = 32767            # Excel's own limit for the text of one cell
MAX_ROW = 1048576
MAX_COL = 16384
COM_TIMEOUT = 90                  # seconds one COM call may run before it is abandoned
CONVERT_TIMEOUT = 180             # seconds one LibreOffice conversion may run

SHEET_EXT = {".xlsx", ".xlsm", ".xltx"}     # readable by sheet_list and sheet_read
WRITE_EXT = ".xlsx"                         # the only format sheet_write may change
# Formats that can carry macros. Refused for writing; some are still readable.
MACRO_EXT = frozenset({
    ".xlsm", ".xlsb", ".xltm", ".xlam", ".xla", ".xls",
    ".docm", ".dotm", ".pptm", ".potm", ".ppam", ".ppsm",
})
CONVERTIBLE = {".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".odt", ".ods", ".rtf", ".txt"}

# Environment names a child process must never inherit.
_SECRET_MARKERS = (
    "TOKEN", "KEY", "SECRET", "PASS", "CREDENTIAL", "API",
    "TELEGRAM", "OPENAI", "ANTHROPIC", "DEEPSEEK",
)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_A1 = re.compile(r"([A-Za-z]{1,3})([1-9][0-9]{0,6})")
_PAGE_TOKEN = re.compile(r"([0-9]{1,6})(?:-([0-9]{1,6}))?")

_abandoned = 0
_abandoned_lock = threading.Lock()


def _err(message: str, code: str = "office_failed") -> dict:
    """The failure shape shared by every operation: a message for the owner, a code for policy."""
    return {"error": message, "code": code}


# --------------------------------------------------------------- backends

def has_openpyxl() -> bool:
    import importlib.util
    return bool(importlib.util.find_spec("openpyxl"))


def com_apps() -> list[str]:
    """Which Office applications are actually registered on this machine."""
    if os.name != "nt":
        return []
    import importlib.util
    if not importlib.util.find_spec("win32com"):
        return []
    import winreg

    found = []
    for prog in ("Word.Application", "Excel.Application", "PowerPoint.Application"):
        try:
            winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog).Close()
            found.append(prog.split(".")[0])
        except OSError:
            continue
    return found


def libreoffice_path() -> str:
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        candidate = Path(os.environ.get(var, "")) / "LibreOffice" / "program" / "soffice.exe"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("soffice") or shutil.which("libreoffice") or ""


def capabilities() -> dict:
    """Reported to the user so a missing backend is visible, not mysterious."""
    apps = com_apps()
    lo = libreoffice_path()
    return {
        "read_sheets": has_openpyxl(),
        "office_com": apps,
        "libreoffice": bool(lo),
        "pdf_to_pdf": bool(apps) or bool(lo),
        "pdf_pages": _has_fitz(),
        "com_abandoned": com_abandoned(),
    }


def _has_fitz() -> bool:
    import importlib.util
    return bool(importlib.util.find_spec("pymupdf") or importlib.util.find_spec("fitz"))


def _pymupdf():
    """`fitz` is the legacy alias and now warns on import."""
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        import fitz
        return fitz


def _open_read(p: Path):
    from openpyxl import load_workbook

    return load_workbook(p, read_only=True, data_only=True)


# ------------------------------------------------------------------- read

def sheet_list(path: str) -> dict:
    """Sheet names with their used size - cheap way to pick the right tab."""
    p = Path(path)
    if p.suffix.lower() not in SHEET_EXT:
        return _err(f"Bu Excel fayli emas: {p.suffix}", "arg_invalid")
    if not has_openpyxl():
        return _err("openpyxl o'rnatilmagan", "not_configured")
    try:
        wb = _open_read(p)
    except Exception as exc:
        return _err(f"Ochib bo'lmadi: {exc}", "arg_invalid")
    try:
        return {
            "path": str(p),
            "sheets": [
                {"name": ws.title, "rows": ws.max_row or 0, "cols": ws.max_column or 0}
                for ws in wb.worksheets
            ],
        }
    finally:
        wb.close()


def sheet_read(
    path: str,
    sheet: str | None = None,
    max_rows: int = MAX_ROWS,
    max_cols: int = MAX_COLS,
) -> dict:
    """A worksheet as a compact text table, with formulas already evaluated.

    ``data_only=True`` returns the value Excel last cached, not the formula -
    which is what a person asking "how much was the profit" actually wants.
    """
    p = Path(path)
    if p.suffix.lower() not in SHEET_EXT:
        return _err(f"Bu Excel fayli emas: {p.suffix}", "arg_invalid")
    if not has_openpyxl():
        return _err("openpyxl o'rnatilmagan", "not_configured")
    try:
        wb = _open_read(p)
    except Exception as exc:
        return _err(f"Ochib bo'lmadi: {exc}", "arg_invalid")

    try:
        if sheet:
            match = _find_sheet(wb, sheet)
            if match is None:
                return _err(f"«{sheet}» varag'i yo'q. Bor: {', '.join(wb.sheetnames)}", "arg_invalid")
            ws = match
        else:
            ws = wb.worksheets[0]

        rows: list[list[str]] = []
        truncated_cols = False
        for row in ws.iter_rows(max_row=max_rows + 1, max_col=max_cols + 1, values_only=True):
            if len(rows) >= max_rows:
                break
            if row and len(row) > max_cols:
                truncated_cols = True
                row = row[:max_cols]
            cells = ["" if v is None else _fmt(v) for v in (row or ())]
            if any(cells):
                rows.append(cells)

        return {
            "path": str(p),
            "sheet": ws.title,
            "all_sheets": wb.sheetnames,
            "table": _as_text(rows),
            "shown_rows": len(rows),
            "total_rows": ws.max_row or 0,
            "truncated": (ws.max_row or 0) > max_rows or truncated_cols,
        }
    except Exception as exc:
        return _err(f"O'qishda xato: {exc}")
    finally:
        wb.close()


def _find_sheet(wb, wanted: str):
    """Match a sheet name across scripts - the tab may be Cyrillic."""
    target = normalize(wanted)
    for ws in wb.worksheets:                      # exact title, or the same name normalised
        if ws.title == wanted or normalize(ws.title) == target:
            return ws
    for ws in wb.worksheets:                      # then a loose contains-match
        if target and target in normalize(ws.title):
            return ws
    return None


def _fmt(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and abs(value) >= 10000:
        return f"{value:,}".replace(",", " ")     # 1 240 000 000 reads far better
    return str(value)


def _as_text(rows: list[list[str]]) -> str:
    """Pipe-separated, column-aligned - compact for a model and for a phone."""
    if not rows:
        return "(bo'sh)"
    # iter_rows pads every row out to max_col, so drop the trailing empty
    # columns before measuring - otherwise a two-column sheet renders with a
    # dozen empty pipes and burns tokens for nothing.
    width = 0
    for r in rows:
        last = max((i + 1 for i, c in enumerate(r) if c), default=0)
        width = max(width, last)
    if not width:
        return "(bo'sh)"
    rows = [(r + [""] * width)[:width] for r in rows]
    widths = [min(24, max(len(r[i]) for r in rows)) for i in range(width)]
    return "\n".join(
        " | ".join(cell[:24].ljust(w) for cell, w in zip(row, widths)).rstrip()
        for row in rows
    )


# ------------------------------------------------------- produce new files

def scratch_dir() -> Path:
    """COWORKER_HOME/scratch: the only folder that generated files are written to."""
    from .config import config_dir

    d = config_dir() / "scratch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def to_pdf(path: str) -> dict:
    """Render a document to PDF in scratch. The source is never changed or written beside."""
    src = Path(path)
    if not src.is_file():
        return _err(f"Fayl yo'q: {path}", "arg_invalid")
    ext = src.suffix.lower()
    if ext != ".pdf" and ext not in CONVERTIBLE:
        return _err(f"Bu format PDF'ga o'girilmaydi: {ext}", "arg_invalid")
    try:
        final = _scratch_file(src, f"{src.stem}.pdf")
        if _is_fresh(final, src):
            return {"ok": True, "path": str(final), "reused": True}
        if ext == ".pdf":
            return _copy_pdf(src, final)
        return _render(src, final)
    except OSError as exc:
        return _err(f"Fayl bilan ishlashda xato: {exc}")


def _render(src: Path, final: Path) -> dict:
    """Office over COM first, LibreOffice second. The file appears only when complete.

    A COM call that overruns ends the chain: the abandoned call may still be
    writing its part file, so LibreOffice must not race it for the same output.
    """
    part = _part_name(final)
    com = _run_bounded(_pdf_via_com, COM_TIMEOUT, src, part)
    if com.get("code") == "timeout":
        return com
    if com.get("ok"):
        return _publish(part, final, com)
    _discard(part)
    lo = _pdf_via_libreoffice(src, final)
    if lo.get("ok"):
        return lo
    if com.get("code") == "not_configured" and lo.get("code") == "not_configured":
        return _err(
            "PDF'ga o'girish uchun Microsoft Office yoki LibreOffice kerak. "
            "Bu kompyuterda ikkalasi ham yo'q.",
            "not_configured",
        )
    return com if lo.get("code") == "not_configured" else lo


def _copy_pdf(src: Path, final: Path) -> dict:
    """A PDF asked for as a PDF: copy it into scratch, so the owner gets a file, not the original."""
    part = _part_name(final)
    try:
        shutil.copyfile(src, part)
    except OSError as exc:
        _discard(part)
        return _err(f"PDF nusxasini olib bo'lmadi: {exc}")
    return _publish(part, final, {"ok": True, "via": "copy"})


def _pdf_via_com(src: Path, target: Path) -> dict:
    """Word/Excel/PowerPoint via COM. Highest fidelity when Office is present.

    Runs on a worker thread under _run_bounded; the caller decides what a
    failure means for the chain of backends.
    """
    apps = com_apps()
    ext = src.suffix.lower()
    kind = (
        "Word" if ext in {".docx", ".doc", ".rtf", ".odt", ".txt"}
        else "Excel" if ext in {".xlsx", ".xls", ".ods"}
        else "PowerPoint" if ext in {".pptx", ".ppt"}
        else ""
    )
    if not kind or kind not in apps:
        return _err(f"{kind or ext} uchun COM yo'q", "not_configured")

    import pythoncom
    import win32com.client

    # Tool calls run on a worker thread; COM demands per-thread initialisation.
    pythoncom.CoInitialize()
    app = None
    doc = None
    try:
        app = win32com.client.DispatchEx(f"{kind}.Application")
        try:
            app.Visible = False
        except Exception:
            pass                                   # PowerPoint refuses to hide
        try:
            app.DisplayAlerts = False
        except Exception:
            pass

        full_src, full_target = str(src.resolve()), str(target.resolve())
        if kind == "Word":
            doc = app.Documents.Open(full_src, ReadOnly=True)
            doc.ExportAsFixedFormat(full_target, 17)          # 17 = wdExportFormatPDF
        elif kind == "Excel":
            doc = app.Workbooks.Open(full_src, ReadOnly=True)
            doc.ExportAsFixedFormat(0, full_target)           # 0 = xlTypePDF
        else:
            doc = app.Presentations.Open(full_src, ReadOnly=True, WithWindow=False)
            doc.SaveAs(full_target, 32)                       # 32 = ppSaveAsPDF

        if not target.is_file():
            return _err("Office xatosiz tugadi, lekin fayl yaratilmadi")
        return {"ok": True, "path": str(target), "via": f"Office/{kind}"}

    except Exception as exc:
        return _err(f"COM: {exc}")
    finally:
        # Leaking a hidden EXCEL.EXE on every call is the classic failure here.
        for obj, method in ((doc, "Close"), (app, "Quit")):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:
                    pass
        pythoncom.CoUninitialize()


def _pdf_via_libreoffice(src: Path, final: Path) -> dict:
    """LibreOffice names its output after the input, so it renders into a private folder first."""
    exe = libreoffice_path()
    if not exe:
        return _err("LibreOffice yo'q", "not_configured")
    out_dir = final.parent / f".lo-{uuid.uuid4().hex[:8]}"
    out_dir.mkdir()
    try:
        try:
            code, stderr = _run_child(
                [exe, "--headless", "--norestore", "--convert-to", "pdf",
                 "--outdir", str(out_dir), str(src.resolve())],
                cwd=final.parent, timeout=CONVERT_TIMEOUT,
            )
        except OSError as exc:
            return _err(f"LibreOffice ishga tushmadi: {exc}")
        if code is None:
            return _err("LibreOffice javob bermadi", "timeout")
        produced = out_dir / f"{src.stem}.pdf"
        if not produced.is_file():
            return _err(stderr[:200] or "natija yo'q")
        try:
            os.replace(produced, final)
        except OSError as exc:
            return _err(f"Natijani saqlab bo'lmadi: {exc}")
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    return {"ok": True, "path": str(final), "reused": False, "via": "LibreOffice"}


def pdf_pages(path: str, pages: str) -> dict:
    """Pull a page range out of a PDF into scratch - "send me just page 3" on a phone.

    Pages keep the order asked for. The output is named from the parsed page
    list, never from the raw text, so the owner's input cannot steer the name.
    """
    src = Path(path)
    if not src.is_file() or src.suffix.lower() != ".pdf":
        return _err("PDF fayl kerak", "arg_invalid")
    if not _has_fitz():
        return _err("PyMuPDF o'rnatilmagan", "not_configured")

    fitz = _pymupdf()
    try:
        with fitz.open(src) as doc:
            total = doc.page_count
            wanted = _parse_pages(pages, total)
            if wanted is None:
                return _err(f"Sahifa raqami noto'g'ri: «{pages}». Masalan: 1-3,7", "arg_invalid")
            if not wanted:
                return _err(f"Sahifa raqami noto'g'ri. Faylda {total} sahifa bor.", "arg_invalid")
            final = _scratch_file(src, f"{src.stem}_p{_page_token(wanted)}.pdf")
            if _is_fresh(final, src):
                return {"ok": True, "path": str(final), "pages": len(wanted), "of": total, "reused": True}
            part = _part_name(final)
            try:
                with fitz.open() as new:
                    for index in wanted:
                        new.insert_pdf(doc, from_page=index, to_page=index)
                    new.save(str(part))
            except Exception:
                _discard(part)
                raise
            return _publish(part, final, {"ok": True, "pages": len(wanted), "of": total})
    except Exception as exc:
        return _err(f"PDF xatosi: {exc}")


def _parse_pages(spec: str, total: int) -> list[int] | None:
    """"1-3,7" -> [0, 1, 2, 6]. One-based in, zero-based out, clamped to the document.

    The order asked for is kept. A malformed token makes the whole spec invalid
    (None): skipping it would quietly send different pages from the ones named.
    """
    out: list[int] = []
    seen: set[int] = set()
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        m = _PAGE_TOKEN.fullmatch(part)
        if m is None:
            return None
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if lo > hi:
            lo, hi = hi, lo
        for n in range(max(lo, 1), min(hi, total) + 1):
            if n not in seen:
                seen.add(n)
                out.append(n - 1)
    return out


def _page_token(indices: list[int]) -> str:
    """Page list as a name fragment: [0, 1, 2, 6] -> "1-3_7". Very long lists fall back to a digest."""
    runs: list[list[int]] = []
    for index in indices:
        n = index + 1
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    token = "_".join(f"{a}-{b}" if b > a else str(a) for a, b in runs)
    if len(token) <= 40:
        return token
    return f"{len(indices)}pages-{hashlib.sha256(token.encode()).hexdigest()[:8]}"


# ----------------------------------------------------- scratch and freshness

def _scratch_file(src: Path, name: str) -> Path:
    """Where a generated file for this exact version of ``src`` goes.

    The folder key is the resolved path plus size and mtime: two files with the
    same name never share output, and an edited source gets a fresh folder.
    """
    st = src.stat()
    raw = f"{os.path.normcase(str(src.resolve()))}|{st.st_size}|{st.st_mtime_ns}"
    folder = scratch_dir() / hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    folder.mkdir(parents=True, exist_ok=True)
    return folder / name


def _is_fresh(target: Path, source: Path) -> bool:
    """Reuse a scratch file only if it is non-empty and not older than its source."""
    try:
        made = target.stat()
        return made.st_size > 0 and made.st_mtime_ns >= source.stat().st_mtime_ns
    except OSError:
        return False


def _part_name(final: Path) -> Path:
    """A private name beside the final file, published by one rename once complete."""
    return final.with_name(f"{final.stem}.{uuid.uuid4().hex[:8]}.part.pdf")


def _publish(part: Path, final: Path, result: dict) -> dict:
    """Move a finished part file to its final name. Until this rename nothing can be reused."""
    try:
        os.replace(part, final)
    except OSError as exc:
        _discard(part)
        return _err(f"Natijani saqlab bo'lmadi: {exc}")
    return {**result, "path": str(final), "reused": False}


def _discard(part: Path) -> None:
    """Remove a part file this module created. It is a copy, never the owner's original."""
    try:
        part.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not remove part file %s: %s", part.name, exc)


def _run_bounded(fn: Callable[..., dict], timeout: float, *args: Any) -> dict:
    """Run one COM call on a worker thread and wait at most ``timeout`` seconds.

    COM cannot be interrupted from outside, so an overrunning call keeps its
    thread. The caller gets a timeout error at once and the late result is
    dropped. The number of such calls is reported by capabilities().
    """
    global _abandoned
    box: dict[str, dict] = {}

    def work() -> None:
        try:
            box["result"] = fn(*args)
        except Exception as exc:
            box["result"] = _err(f"COM: {exc}")

    worker = threading.Thread(target=work, name="office-com", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        with _abandoned_lock:
            _abandoned += 1
        log.warning("COM call overran %s s and was abandoned", timeout)
        return _err(f"Office {timeout:g} soniya ichida javob bermadi", "timeout")
    return box.get("result", _err("COM: natija yo'q"))


def com_abandoned() -> int:
    """How many COM calls overran and were left running in the background."""
    with _abandoned_lock:
        return _abandoned


def _child_env() -> dict[str, str]:
    """What a child may inherit: the environment minus anything named like a credential."""
    return {k: v for k, v in os.environ.items() if not any(m in k.upper() for m in _SECRET_MARKERS)}


def _run_child(argv: list[str], *, cwd: Path, timeout: float) -> tuple[int | None, str]:
    """Run a child with a scrubbed environment. Returns (exit code, stderr); code is None on overrun."""
    proc = subprocess.Popen(
        argv, cwd=str(cwd), env=_child_env(),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    try:
        _, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        return None, ""
    return proc.returncode, (err or b"").decode(errors="replace")


def _kill_tree(proc: subprocess.Popen) -> None:
    """soffice.exe hands its work to soffice.bin, so killing only the parent leaves work running."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, creationflags=_NO_WINDOW, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("taskkill failed for pid %s: %s", proc.pid, exc)
    proc.kill()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        log.warning("child pid %s did not exit after kill", proc.pid)
    if proc.stderr:
        proc.stderr.close()


# ---------------------------------------------------------------- modify

def sheet_write(path: str, changes: dict, sheet: str | None = None) -> dict:
    """Set cells, e.g. {"B5": 500000}. Backs the file up first and swaps the save in atomically.

    openpyxl round-trips a workbook by rewriting it, which can drop charts,
    images and some formatting. The backup is not a nicety - it is the undo.
    """
    p = Path(path)
    problem = validate_write(path, changes, sheet)
    if problem:
        return _err(problem, "arg_invalid")

    backup = _backup(p)
    if "error" in backup:
        return backup

    try:
        from openpyxl import load_workbook

        wb = load_workbook(p)                       # keep formulas, so not data_only
        name = _match_sheet(wb.sheetnames, sheet) if sheet else wb.sheetnames[0]
        ws = wb[name]
        applied = {}
        for ref, value in changes.items():
            cell = _cell_ref(ref)
            ws[cell] = _coerce(value)
            applied[cell] = value
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()
    except Exception as exc:
        return {**_err(f"Yozishda xato: {exc}"), "backup": backup["backup"]}

    try:
        _replace_file(p, buf.getvalue())
    except OSError as exc:
        return {**_err(f"Faylni yangilab bo'lmadi, asl fayl o'zgarmadi: {exc}"), "backup": backup["backup"]}
    return {"ok": True, "path": str(p), "sheet": name, "applied": applied, "backup": backup["backup"]}


def validate_write(path: str, changes: Any, sheet: str | None = None) -> str | None:
    """Everything sheet_write refuses, decided without writing anything.

    The policy hook calls this before the owner is asked to confirm, so a call
    that would fail anyway never produces a confirmation card.
    """
    p = Path(path)
    refusal = _write_refusal(p)
    if refusal:
        return refusal
    shape = check_changes(changes)
    if shape:
        return shape
    if not p.is_file():
        return f"Fayl yo'q: {path}"
    if not has_openpyxl():
        return "openpyxl o'rnatilmagan"
    try:
        wb = _open_read(p)
    except Exception as exc:
        return f"Ochib bo'lmadi: {exc}"
    try:
        names = list(wb.sheetnames)
    finally:
        wb.close()
    if sheet and _match_sheet(names, sheet) is None:
        return f"«{sheet}» varag'i yo'q. Bor: {', '.join(names)}"
    return None


def check_changes(changes: Any) -> str | None:
    """Shape rules for a cell map: one to MAX_CHANGES A1 cells, each holding one scalar."""
    if not isinstance(changes, dict) or not changes:
        return "O'zgarishlar ko'rsatilmagan"
    if len(changes) > MAX_CHANGES:
        return f"Bir marta {MAX_CHANGES} tadan ko'p katak o'zgartirilmaydi"
    seen: set[str] = set()
    for ref, value in changes.items():
        cell = _cell_ref(ref)
        if cell is None:
            return f"Noto'g'ri katak manzili: {str(ref)[:20]}. Masalan: B5"
        if cell in seen:
            return f"Bir katak ikki marta berilgan: {cell}"
        seen.add(cell)
        if value is not None and not isinstance(value, (str, int, float, bool)):
            return f"{cell}: qiymat matn, son yoki bo'sh bo'lishi kerak"
        if isinstance(value, float) and not math.isfinite(value):
            return f"{cell}: qiymat cheksiz yoki NaN bo'lishi mumkin emas"
        if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
            return f"{cell}: matn juda uzun (ko'pi bilan {MAX_CELL_CHARS} belgi)"
    return None


def _write_refusal(p: Path) -> str | None:
    ext = p.suffix.lower()
    if ext in MACRO_EXT:
        return f"Makrosli fayl ({ext}) o'zgartirilmaydi: saqlashda makrolar yo'qolishi mumkin"
    if ext != WRITE_EXT:
        return "Faqat .xlsx fayllarini tahrirlay olaman"
    return None


def _cell_ref(ref: Any) -> str | None:
    """Canonical A1 form ("b5" -> "B5"), or None if it is not one cell inside Excel's grid."""
    m = _A1.fullmatch(str(ref))
    if m is None:
        return None
    letters = m.group(1).upper()
    column = 0
    for ch in letters:
        column = column * 26 + (ord(ch) - ord("A") + 1)
    row = int(m.group(2))
    if column > MAX_COL or row > MAX_ROW:
        return None
    return f"{letters}{row}"


def _match_sheet(names: list[str], wanted: str) -> str | None:
    """Writes need the sheet that was named. Match the normalised name, never a loose substring."""
    target = normalize(wanted)
    for name in names:
        if name == wanted or normalize(name) == target:
            return name
    return None


def _coerce(value: Any) -> Any:
    """A model sends JSON; a spreadsheet wants numbers to stay numbers.

    Text that only looks numeric becomes a number. "nan" and "inf" stay text,
    because a non-finite number is not a valid cell value in a workbook.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value).strip()
    cleaned = text.replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return int(cleaned)
    except ValueError:
        pass
    try:
        number = float(cleaned)
    except ValueError:
        return text
    return number if math.isfinite(number) else text


def _backup(p: Path) -> dict:
    """Copy the workbook beside itself under a name that is never reused.

    The copy is created exclusively, so two writes in the same second get -1 and
    -2 rather than one overwriting the other's undo copy.
    """
    stamp = _stamp()
    for n in range(1, 1000):
        target = p.with_name(f"{p.stem}.backup-{stamp}-{n}{p.suffix}")
        try:
            dst = open(target, "xb")
        except FileExistsError:
            continue
        except OSError as exc:
            return _err(f"Zaxira nusxa olinmadi, o'zgartirmadim: {exc}")
        try:
            with dst, open(p, "rb") as src:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            shutil.copystat(p, target)
        except OSError as exc:
            _discard(target)                         # our own partial copy
            return _err(f"Zaxira nusxa olinmadi, o'zgartirmadim: {exc}")
        return {"backup": str(target)}
    return _err("Zaxira nusxa uchun bo'sh nom topilmadi, o'zgartirmadim")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _replace_file(target: Path, data: bytes) -> None:
    """Write beside the target, flush to disk, then swap it in with one rename.

    A failed write leaves the original untouched; the temporary file is ours
    and is removed when anything goes wrong.
    """
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.stem}-", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        _discard(tmp)
        raise


def _work_dir() -> Path:
    """Generated files for other modules (browser screenshots). Office output uses scratch_dir()."""
    from .config import config_dir

    d = config_dir() / "generated"
    d.mkdir(parents=True, exist_ok=True)
    _prune(d)
    return d


def _prune(d: Path, keep_hours: int = 48) -> None:
    cutoff = time.time() - keep_hours * 3600
    try:
        for f in d.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass
