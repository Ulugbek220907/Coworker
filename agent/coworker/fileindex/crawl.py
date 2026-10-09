"""The crawl: walks the index roots and hands each file to the index.

The walk keeps an explicit stack, so depth is not limited by the recursion
limit. Every filesystem step (listing a folder, extracting one file's text) runs
through a governor-like object, ``governor.run("INDEX", fn, timeout_s=..., cancel=...)``.
The real governor is a coroutine and is driven here on a private event loop, so
the crawl runs in a plain thread. Tests pass a fake with a synchronous ``run``.

When the governor refuses with ``paused``, ``throttled`` or ``low_memory``, the
crawl waits and retries the same step. It never skips ahead while paused.

Placeholder files (OneDrive "online only" files) are never opened. Their name
and metadata are indexed, and their text is not, because opening one downloads it.

Files the policy blocks by name (passwords, tokens, ``.env``) and folders under a
protected root (the Coworker home, ``.ssh``) are skipped entirely: no name, no
text, no row. The same two tests decide what search may return, so a row written
before a rule existed is hidden from search at once and removed by the next
complete pass.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import threading
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional

from .. import fs
from ..core.types import CancelToken
from ..extract import SUPPORTED, extract
from ..governor.model import GovTimeout, Refused
from ..policy import paths

log = logging.getLogger("fileindex")

MAX_DEPTH = 10
EXCLUDED_DIRS = frozenset({"node_modules", ".git", "__pycache__", "site-packages", "appdata", "windows"})
PAUSE_CODES = frozenset({"paused", "throttled", "low_memory"})
GOV_CLASS = "INDEX"
SCAN_TIMEOUT_S = 30.0
EXTRACT_TIMEOUT_S = 60.0

# Windows file attributes that mark a file as a cloud placeholder.
ATTR_OFFLINE = 0x1000
ATTR_RECALL_ON_OPEN = 0x40000
ATTR_RECALL_ON_DATA_ACCESS = 0x400000
PLACEHOLDER_ATTRIBUTES = ATTR_OFFLINE | ATTR_RECALL_ON_OPEN | ATTR_RECALL_ON_DATA_ACCESS


class BudgetExceeded(Exception):
    """A write would go past a budget. ``reason`` is one of rows, db_size, day_writes, hour_writes."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Stop(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ScanEntry:
    name: str
    path: str
    is_dir: bool
    size: int
    mtime: float
    attrs: int


@dataclass
class FileRecord:
    path: str
    name: str
    folder: str
    size: int
    mtime: float
    kind: str
    placeholder: bool = False
    text: str = ""
    needs_text: bool = False  # text extraction timed out; retry on the next pass


@dataclass
class CrawlReport:
    dirs_scanned: int = 0
    files_seen: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    text_extracted: int = 0
    placeholders: int = 0
    refused: int = 0            # files and folders the policy skipped
    pauses: int = 0
    timeouts: int = 0
    scan_timeouts: int = 0
    errors: int = 0
    stopped: Optional[str] = None  # None when the walk finished

    def as_dict(self) -> dict:
        return asdict(self)


def attributes_of(entry: os.DirEntry) -> int:
    """The Windows attribute word of a directory entry, 0 when unavailable."""
    try:
        return int(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) or 0)
    except OSError:
        return 0


def is_placeholder(attrs: int) -> bool:
    return bool(attrs & PLACEHOLDER_ATTRIBUTES)


def protected_roots() -> frozenset[str]:
    """The protected roots (see policy.paths), resolved for the current environment.

    Resolving them costs about 10 ms, so a pass or a search asks once and passes
    the result to ``folder_refused``.
    """
    return frozenset(paths._protected_roots(write=False))


def folder_refused(path: str, roots: frozenset[str]) -> bool:
    """True when the folder lies inside a protected root, such as a Coworker home under a user folder.

    Only the folder's place is judged. ``paths.check_path`` also refuses every
    reparse point on the way down, and an online-only OneDrive folder carries that
    attribute, so its files would vanish from search. Links are excluded at scan
    time instead.
    """
    key = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    return any(paths._inside(key, root) for root in roots)


def file_refused(name: str) -> bool:
    """True when the policy blocks a file by its name, the same test preview_file applies."""
    return paths._blocked(name)


def scan_dir(path: str) -> list[ScanEntry]:
    """One folder level: the entries the index cares about. Links and excluded folders are dropped."""
    out: list[ScanEntry] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if fs.is_link_entry(entry) or entry.name.lower() in EXCLUDED_DIRS:
                            continue
                        out.append(ScanEntry(entry.name, entry.path, True, 0, 0.0, 0))
                    elif entry.is_file(follow_symlinks=False):
                        if fs.is_link_entry(entry):
                            continue
                        st = entry.stat(follow_symlinks=False)
                        out.append(ScanEntry(
                            entry.name, entry.path, False, st.st_size, st.st_mtime, attributes_of(entry),
                        ))
                except OSError:
                    continue
    except OSError:
        return out
    return out


class Crawler:
    """One pass over the roots. Build a new one for each pass."""

    def __init__(
        self,
        index: Any,
        roots: list[str],
        *,
        governor: Any = None,
        stop: Optional[threading.Event] = None,
        pause_s: float = 15.0,
        max_file_bytes: int = 45 * 1024 * 1024,
        max_text_bytes: int = 200 * 1024,
    ) -> None:
        self.index = index
        self.roots = list(roots)
        self.governor = governor
        self.stop = stop or threading.Event()
        self.cancel = CancelToken()
        self.pause_s = pause_s
        self.max_file_bytes = max_file_bytes
        self.max_text_bytes = max_text_bytes
        self.report = CrawlReport()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._protected = protected_roots()

    # ------------------------------------------------------------------ pass

    def run(self) -> CrawlReport:
        self.index.begin_pass()
        self._loop = asyncio.new_event_loop()
        try:
            self._walk()
        except _Stop as stop:
            self.report.stopped = stop.reason
        except Refused as exc:
            # A refusal that is not a pause (for example locked_desktop) ends the pass.
            self.report.stopped = exc.code
        finally:
            try:
                self.index.flush()
            finally:
                self._loop.close()
                self._loop = None
        # Files that were never seen must not be pruned from the index when a folder
        # listing timed out or a step failed: the pass did not really see them.
        complete = self.report.stopped is None and self.report.scan_timeouts == 0 and self.report.errors == 0
        self.index.finish_pass(complete, self.report)
        return self.report

    def _walk(self) -> None:
        seen: set[str] = set()
        stack: list[tuple[str, int]] = []
        for root in self.roots:
            if not os.path.isdir(root):
                continue
            key = fs._key(root)
            if key not in seen:
                seen.add(key)
                stack.append((root, 0))

        while stack:
            self._check_stop()
            reason = self.index.check_budget(0, False)
            if reason:
                raise _Stop(reason)
            path, depth = stack.pop()
            if folder_refused(path, self._protected):
                self.report.refused += 1
                continue
            entries = self._govern(lambda p=path: scan_dir(p), SCAN_TIMEOUT_S, label="scan")
            if entries is None:
                self.report.scan_timeouts += 1
                continue
            self.report.dirs_scanned += 1
            for entry in entries:
                if entry.is_dir:
                    if depth + 1 > MAX_DEPTH:
                        continue
                    key = fs._key(entry.path)
                    if key in seen:
                        continue
                    seen.add(key)
                    stack.append((entry.path, depth + 1))
                else:
                    self._file(entry)

    # ----------------------------------------------------------------- files

    def _file(self, entry: ScanEntry) -> None:
        self.report.files_seen += 1
        if file_refused(entry.name):
            self.report.refused += 1
            return
        try:
            known = self.index.known(entry.path)
            if known is not None and known[:2] == (entry.size, entry.mtime) and not known[2]:
                self.index.touch(entry.path)
                self.report.unchanged += 1
                return

            placeholder = is_placeholder(entry.attrs)
            ext = os.path.splitext(entry.name)[1].lower()
            text = ""
            needs_text = False
            if placeholder:
                self.report.placeholders += 1
            elif ext in SUPPORTED and entry.size <= self.max_file_bytes:
                extracted = self._govern(
                    lambda p=entry.path: extract(p, limit=self.max_text_bytes),
                    EXTRACT_TIMEOUT_S,
                    label="extract",
                )
                if extracted is None:
                    needs_text = True  # timed out: the next pass tries this file again
                elif extracted:
                    text = extracted
                    self.report.text_extracted += 1

            record = FileRecord(
                path=entry.path,
                name=entry.name,
                folder=os.path.dirname(entry.path),
                size=entry.size,
                mtime=entry.mtime,
                kind=kind_of(entry.name),
                placeholder=placeholder,
                text=text,
                needs_text=needs_text,
            )
            # upsert checks the budgets itself, before it writes anything.
            status = self.index.upsert(record)
            if status == "added":
                self.report.added += 1
            else:
                self.report.updated += 1
        except BudgetExceeded as exc:
            raise _Stop(exc.reason) from exc
        except _Stop:
            raise
        except Exception:
            self.report.errors += 1
            log.debug("index step failed for a file", exc_info=True)

    # --------------------------------------------------------------- governor

    def _govern(self, fn: Callable[[], Any], timeout_s: float, *, label: str) -> Any:
        """Run one step under the governor, waiting out pauses. None when the step timed out."""
        while True:
            self._check_stop()
            try:
                return self._call(fn, timeout_s)
            except Refused as exc:
                if exc.code not in PAUSE_CODES:
                    raise
                self.report.pauses += 1
                self.index.set_state("paused", exc.code)
                if self.stop.wait(self.pause_s):
                    raise _Stop("stopped")
                self.index.set_state("crawling")
            except GovTimeout:
                self.report.timeouts += 1
                log.debug("index %s step timed out", label)
                return None

    def _call(self, fn: Callable[[], Any], timeout_s: float) -> Any:
        if self.governor is None:
            return fn()
        result = self.governor.run(GOV_CLASS, fn, timeout_s=timeout_s, cancel=self.cancel)
        if inspect.isawaitable(result):
            return self._loop.run_until_complete(result)
        return result

    def _check_stop(self) -> None:
        if self.stop.is_set():
            self.cancel.cancel()
            raise _Stop("stopped")


_KIND_BY_EXT = {
    ".pdf": "pdf",
    ".docx": "document", ".docm": "document", ".doc": "document", ".odt": "document",
    ".rtf": "document", ".pages": "document", ".txt": "text", ".md": "text", ".log": "text",
    ".xlsx": "spreadsheet", ".xlsm": "spreadsheet", ".xls": "spreadsheet", ".ods": "spreadsheet",
    ".csv": "spreadsheet", ".tsv": "spreadsheet", ".numbers": "spreadsheet",
    ".pptx": "presentation", ".pptm": "presentation", ".ppt": "presentation", ".odp": "presentation",
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".heic": "image", ".tif": "image", ".tiff": "image",
    ".zip": "archive", ".rar": "archive", ".7z": "archive",
    ".lnk": "shortcut", ".url": "shortcut", ".appref-ms": "shortcut",
}


def kind_of(name: str) -> str:
    """A coarse file kind from the extension, for display and for the model."""
    return _KIND_BY_EXT.get(os.path.splitext(name)[1].lower(), "other")
