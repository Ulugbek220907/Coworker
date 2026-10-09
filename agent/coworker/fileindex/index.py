"""The file index: names and extracted text for the user's own folders.

The index is its own SQLite file, separate from the agent's store, so a corrupt
or oversized index cannot touch conversations, approvals or the audit chain. It
holds three tables and one virtual table:

* ``files``  one row per file: path key, name, size, mtime, kind, placeholder flag
* ``texts``  extracted text of a file, up to 200 KB, raw and folded
* ``meta``   small counters (write budgets, pass number)
* ``fts``    FTS5 over the folded name and text, when FTS5 is available

Search uses ``fts`` when it exists and falls back to LIKE over the same folded
columns when it does not. When the index has no answer, a bounded live walk
(``fs.find_files``, 8 s, restricted to the index roots) is tried. Neither path
returns a file the policy hides (a blocked name, a folder under a protected root),
even one an older version of the crawl wrote.

Budgets (docs/architecture-v2.md section 8) are checked before every write:
2 GB of database, 500 000 rows, 500 MB of writes per local day and 100 MB per
local hour. Writes are committed every 200 rows.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .. import fs, textutil
from .crawl import (
    BudgetExceeded,
    Crawler,
    CrawlReport,
    FileRecord,
    file_refused,
    folder_refused,
    kind_of,
    protected_roots,
)

log = logging.getLogger("fileindex")

USER_FOLDERS = ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")
LIVE_DEADLINE_S = 8.0
CANDIDATE_LIMIT = 500
MIN_NAME_SCORE = 0.35   # name relevance needed to rank on the name alone
CONTENT_SCORE = 0.4     # rank for a match that came from the text or a partial name
_BUDGET_REASONS = frozenset({"rows", "db_size", "day_writes", "hour_writes"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    folder TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    kind TEXT NOT NULL,
    placeholder INTEGER NOT NULL DEFAULT 0,
    has_text INTEGER NOT NULL DEFAULT 0,
    norm_name TEXT NOT NULL,
    needs_text INTEGER NOT NULL DEFAULT 0,
    seen_pass INTEGER NOT NULL DEFAULT 0,
    indexed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS files_mtime ON files(mtime);
CREATE TABLE IF NOT EXISTS texts (
    file_id INTEGER PRIMARY KEY,
    body TEXT NOT NULL,
    norm TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Budgets:
    db_bytes: int = 2 * 1024 ** 3
    rows: int = 500_000
    day_write_bytes: int = 500 * 1024 ** 2
    hour_write_bytes: int = 100 * 1024 ** 2
    commit_every: int = 200
    max_file_bytes: int = 45 * 1024 ** 2
    max_text_bytes: int = 200 * 1024


def default_roots() -> list[str]:
    """The user folders and their OneDrive copies. Never a whole disk."""
    found: list[str] = []

    def add(path: str) -> None:
        if path and os.path.isdir(path) and not any(fs._key(path) == fs._key(p) for p in found):
            found.append(path)

    for path in fs.known_folders().values():
        add(path)
    bases = [os.environ.get(v) for v in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")]
    bases.append(str(Path.home() / "OneDrive"))
    for base in bases:
        if base:
            for name in USER_FOLDERS:
                add(os.path.join(base, name))
    for name in USER_FOLDERS:
        add(str(Path.home() / name))
    return found


class FileIndex:
    """Search and maintenance of the file index. One instance per database file.

    ``governor`` runs the crawl's filesystem steps under the INDEX pool. Without
    one the steps run inline, which is what tests use. ``clock`` drives the
    per-day and per-hour write windows.
    """

    def __init__(
        self,
        db_path: Path | str,
        roots: Optional[list[str]] = None,
        *,
        governor=None,
        budgets: Optional[Budgets] = None,
        clock: Callable[[], float] = time.time,
        use_fts: bool = True,
        pause_s: float = 15.0,
        rescan_s: float = 3600.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.roots = list(roots) if roots is not None else default_roots()
        self.budgets = budgets or Budgets()
        self.clock = clock
        self.pause_s = pause_s
        self.rescan_s = rescan_s
        self._governor = governor
        self._lock = threading.RLock()
        self._in_tx = False
        self._pending = 0
        self._pass = 0
        self.commits = 0
        self._state = "idle"
        self._state_reason = ""
        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None

        self._conn = sqlite3.connect(
            str(self.db_path), timeout=5.0, isolation_level=None, check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        self.fts_enabled = self._setup_fts(use_fts)
        self._rows = int(self._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        self._pass = self._counter_value("pass")
        self._db_bytes = self._measure_db()

    # ------------------------------------------------------------- lifecycle

    def close(self) -> None:
        with self._lock:
            self._commit()
            self._conn.close()

    def start(self, stop_event: Optional[threading.Event] = None) -> None:
        """Run passes in a daemon thread until ``stop_event`` is set (or :meth:`stop` is called)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event = stop_event or threading.Event()
            self._thread = threading.Thread(
                target=self._loop, args=(self._stop_event,), name="fileindex", daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Set the event given to :meth:`start` and wait for the thread to finish its step."""
        event = self._stop_event
        if event is not None:
            event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None

    def _loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.crawl(stop)
            except Exception:
                log.exception("index pass failed")
            if stop.wait(self.rescan_s):
                break

    def crawl(self, stop: Optional[threading.Event] = None, *, governor=None) -> dict:
        """One full pass over the roots. Returns the pass report."""
        crawler = Crawler(
            self,
            self.roots,
            governor=governor if governor is not None else self._governor,
            stop=stop,
            pause_s=self.pause_s,
            max_file_bytes=self.budgets.max_file_bytes,
            max_text_bytes=self.budgets.max_text_bytes,
        )
        return crawler.run().as_dict()

    # ------------------------------------------------------- crawl interface

    def begin_pass(self) -> int:
        with self._lock:
            self._pass += 1
            self._begin()
            self._set_meta("pass", str(self._pass))
            self._commit()
            self._state = "crawling"
            self._state_reason = ""
            return self._pass

    def finish_pass(self, completed: bool, report: CrawlReport) -> None:
        """Commit the pass. A pass that finished removes rows it did not see again."""
        with self._lock:
            self._begin()
            if completed:
                self._prune_unseen()
            self._set_meta("last_pass_at", repr(self.clock()))
            self._set_meta("last_pass", repr(report.as_dict()))
            self._commit()
            if completed:
                self._state, self._state_reason = "idle", ""
            elif report.stopped in _BUDGET_REASONS:
                self._state, self._state_reason = "budget", report.stopped
            else:
                self._state, self._state_reason = "stopped", report.stopped or ""

    def set_state(self, state: str, reason: str = "") -> None:
        with self._lock:
            self._state = state
            self._state_reason = reason

    def flush(self) -> None:
        with self._lock:
            self._commit()

    def known(self, path: str) -> Optional[tuple[int, float, bool]]:
        """(size, mtime, needs_text) of an indexed file, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT size, mtime, needs_text FROM files WHERE key = ?", (fs._key(path),),
            ).fetchone()
            return None if row is None else (row["size"], row["mtime"], bool(row["needs_text"]))

    def touch(self, path: str) -> None:
        """Mark an unchanged file as seen in this pass."""
        with self._lock:
            self._begin()
            self._conn.execute(
                "UPDATE files SET seen_pass = ? WHERE key = ?", (self._pass, fs._key(path)),
            )
            # Touches count toward the commit cadence too, so a pass of unchanged
            # files does not keep the write lock open until the end.
            self._pending += 1
            if self._pending >= self.budgets.commit_every:
                self._commit()

    def check_budget(self, nbytes: int, new_row: bool) -> Optional[str]:
        """The budget a write of ``nbytes`` would break, or None."""
        with self._lock:
            if new_row and self._rows >= self.budgets.rows:
                return "rows"
            if self._db_bytes >= self.budgets.db_bytes:
                return "db_size"
            now = self.clock()
            if self._counter_value(_day_key(now)) + nbytes > self.budgets.day_write_bytes:
                return "day_writes"
            if self._counter_value(_hour_key(now)) + nbytes > self.budgets.hour_write_bytes:
                return "hour_writes"
            return None

    def upsert(self, rec: FileRecord) -> str:
        """Insert or replace one file. Returns "added" or "updated"; raises BudgetExceeded."""
        with self._lock:
            key = fs._key(rec.path)
            text = _cap_bytes(rec.text, self.budgets.max_text_bytes) if rec.text else ""
            nbytes = len(rec.path.encode("utf-8")) + len(rec.name.encode("utf-8")) + len(text.encode("utf-8"))
            row = self._conn.execute("SELECT id FROM files WHERE key = ?", (key,)).fetchone()
            new_row = row is None
            reason = self.check_budget(nbytes, new_row)
            if reason:
                raise BudgetExceeded(reason)

            self._begin()
            now = self.clock()
            name_norm = textutil.normalize(rec.name)
            body_norm = textutil.translit(text) if text else ""
            values = (
                rec.path, rec.name, rec.folder, rec.size, rec.mtime, rec.kind,
                1 if rec.placeholder else 0, 1 if text else 0, name_norm,
                1 if rec.needs_text else 0, self._pass, now,
            )
            if new_row:
                cur = self._conn.execute(
                    "INSERT INTO files (path, name, folder, size, mtime, kind, placeholder, has_text,"
                    " norm_name, needs_text, seen_pass, indexed_at, key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*values, key),
                )
                file_id = cur.lastrowid
                self._rows += 1
            else:
                file_id = row["id"]
                self._conn.execute(
                    "UPDATE files SET path=?, name=?, folder=?, size=?, mtime=?, kind=?, placeholder=?,"
                    " has_text=?, norm_name=?, needs_text=?, seen_pass=?, indexed_at=? WHERE id=?",
                    (*values, file_id),
                )
                self._conn.execute("DELETE FROM texts WHERE file_id = ?", (file_id,))
                if self.fts_enabled:
                    self._conn.execute("DELETE FROM fts WHERE rowid = ?", (file_id,))

            if text:
                self._conn.execute(
                    "INSERT INTO texts (file_id, body, norm) VALUES (?,?,?)", (file_id, text, body_norm),
                )
            if self.fts_enabled:
                self._conn.execute(
                    "INSERT INTO fts (rowid, name_norm, body_norm) VALUES (?,?,?)",
                    (file_id, name_norm, body_norm),
                )
            self._add_writes(nbytes, now)
            self._pending += 1
            if self._pending >= self.budgets.commit_every:
                self._commit()
            return "added" if new_row else "updated"

    # ---------------------------------------------------------------- search

    def search(self, query: str, limit: int = 12, root: Optional[str] = None) -> dict:
        """Matching files: ``{results, source, truncated}``.

        ``source`` is "index" when the database answered and "live" when the
        bounded walk did. ``root`` restricts both to one folder.
        """
        limit = max(1, min(int(limit or 12), 50))
        q = textutil.prepare(query or "")
        if not q.terms:
            return {"results": [], "source": "index", "truncated": False}
        root = root or None

        ranked = self._index_search(q, root, limit + 1)
        if ranked:
            page = []
            for score, row in ranked[:limit]:
                item = {
                    "name": row["name"],
                    "path": row["path"],
                    "size": row["size"],
                    "mtime": row["mtime"],
                    "kind": row["kind"],
                }
                if row["has_text"]:
                    snip = self._snippet_for(row["id"], q)
                    if snip:
                        item["snippet"] = snip
                page.append(item)
            return {"results": page, "source": "index", "truncated": len(ranked) > limit}
        return self._live_search(q.text, limit, root)

    def list_dir(self, path: str, limit: int = 200) -> dict:
        return fs.list_dir(path, limit=limit)

    def _index_search(self, q: textutil.Query, root: Optional[str], want: int) -> list[tuple[float, sqlite3.Row]]:
        """Up to ``want`` matches the policy shows, best first. Hidden rows are skipped, not counted."""
        with self._lock:
            rows = self._candidates(q)
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            if root and not fs._within(row["path"], root):
                continue
            name_score = textutil.score_expanded(q, os.path.join(os.path.basename(row["folder"]), row["name"]))
            score = name_score if name_score >= MIN_NAME_SCORE else CONTENT_SCORE
            scored.append((score, row))
        scored.sort(key=lambda s: (s[0], s[1]["mtime"]), reverse=True)
        roots, folders = protected_roots(), {}
        shown: list[tuple[float, sqlite3.Row]] = []
        for score, row in scored:
            if _shown(row["name"], row["folder"], roots, folders):
                shown.append((score, row))
                if len(shown) >= want:
                    break
        return shown

    def _candidates(self, q: textutil.Query) -> list[sqlite3.Row]:
        """Rows matching any query variant, through FTS when available, else LIKE."""
        if self.fts_enabled:
            match = " OR ".join('"' + t.replace('"', '""') + '"*' for t in q.terms)
            try:
                return self._conn.execute(
                    "SELECT f.id, f.path, f.name, f.folder, f.size, f.mtime, f.kind, f.has_text"
                    " FROM fts JOIN files f ON f.id = fts.rowid"
                    " WHERE fts MATCH ? ORDER BY bm25(fts, 5.0, 1.0) LIMIT ?",
                    (match, CANDIDATE_LIMIT),
                ).fetchall()
            except sqlite3.OperationalError:
                log.warning("FTS query failed; using LIKE for this search", exc_info=True)

        clauses: list[str] = []
        params: list[str] = []
        for term in q.terms:
            pattern = "%" + _like_escape(term) + "%"
            clauses.append("(f.norm_name LIKE ? ESCAPE '\\' OR COALESCE(t.norm, '') LIKE ? ESCAPE '\\')")
            params += [pattern, pattern]
        sql = (
            "SELECT f.id, f.path, f.name, f.folder, f.size, f.mtime, f.kind, f.has_text"
            " FROM files f LEFT JOIN texts t ON t.file_id = f.id WHERE " + " OR ".join(clauses) + " LIMIT ?"
        )
        return self._conn.execute(sql, (*params, CANDIDATE_LIMIT)).fetchall()

    def _snippet_for(self, file_id: int, q: textutil.Query) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT body FROM texts WHERE file_id = ?", (file_id,)).fetchone()
        if row is None:
            return None
        body = row["body"]
        folded = textutil.translit(body)
        for term in q.terms:
            if term in folded:
                return textutil.snippet(body, term)
        return None

    def _live_search(self, query: str, limit: int, root: Optional[str]) -> dict:
        roots = [root] if root else list(self.roots)
        allowed = [root] if root else (list(self.roots) or None)
        if not roots:
            return {"results": [], "source": "live", "truncated": False}
        found = fs.find_files(
            query, roots, limit=limit + 1, deadline=LIVE_DEADLINE_S, allowed_roots=allowed,
        )
        results: list[dict] = []
        roots, folders = protected_roots(), {}
        for hit in found["results"]:
            if not _shown(hit["name"], os.path.dirname(hit["path"]), roots, folders):
                continue
            try:
                st = os.stat(hit["path"])
            except OSError:
                continue
            results.append({
                "name": hit["name"],
                "path": hit["path"],
                "size": st.st_size,
                "mtime": st.st_mtime,
                "kind": kind_of(hit["name"]),
            })
        return {"results": results[:limit], "source": "live", "truncated": len(results) > limit}

    # ----------------------------------------------------------------- status

    def stats(self) -> dict:
        with self._lock:
            now = self.clock()
            text_rows = int(self._conn.execute("SELECT COUNT(*) FROM texts").fetchone()[0])
            return {
                "files": self._rows,
                "texts": text_rows,
                "db_bytes": self._measure_db(),
                "fts": self.fts_enabled,
                "roots": list(self.roots),
                "state": self._state,
                "reason": self._state_reason,
                "day_writes": self._counter_value(_day_key(now)),
                "hour_writes": self._counter_value(_hour_key(now)),
                "commits": self.commits,
                "pass": self._pass,
                "running": self._thread is not None and self._thread.is_alive(),
            }

    # --------------------------------------------------------------- internals

    def _setup_fts(self, use_fts: bool) -> bool:
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'fts'",
        ).fetchone() is not None
        if not use_fts:
            if exists:
                self._conn.execute("DROP TABLE fts")
            return False
        try:
            self._conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(name_norm, body_norm)")
        except sqlite3.OperationalError:
            log.info("FTS5 is unavailable; search uses LIKE")
            return False
        if not exists:
            # Backfill: rows indexed while FTS was off must be searchable too.
            self._conn.execute(
                "INSERT INTO fts (rowid, name_norm, body_norm)"
                " SELECT f.id, f.norm_name, COALESCE(t.norm, '') FROM files f LEFT JOIN texts t ON t.file_id = f.id"
            )
        return True

    def _begin(self) -> None:
        if not self._in_tx:
            self._conn.execute("BEGIN IMMEDIATE")
            self._in_tx = True

    def _commit(self) -> None:
        if self._in_tx:
            self._conn.execute("COMMIT")
            self._in_tx = False
            self.commits += 1
        self._pending = 0
        self._db_bytes = self._measure_db()

    def _prune_unseen(self) -> None:
        rows = self._conn.execute("SELECT id FROM files WHERE seen_pass < ?", (self._pass,)).fetchall()
        for row in rows:
            self._conn.execute("DELETE FROM texts WHERE file_id = ?", (row["id"],))
            if self.fts_enabled:
                self._conn.execute("DELETE FROM fts WHERE rowid = ?", (row["id"],))
            self._conn.execute("DELETE FROM files WHERE id = ?", (row["id"],))
        self._rows -= len(rows)

    def _add_writes(self, nbytes: int, now: float) -> None:
        for key in (_day_key(now), _hour_key(now)):
            self._set_meta(key, str(self._counter_value(key) + nbytes))
        # Only the current day and hour are budgeted, so older counters go.
        self._conn.execute(
            "DELETE FROM meta WHERE (key LIKE 'day:%' AND key <> ?) OR (key LIKE 'hour:%' AND key <> ?)",
            (_day_key(now), _hour_key(now)),
        )

    def _counter_value(self, key: str) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def _measure_db(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(str(self.db_path) + suffix)
            except OSError:
                pass
        return total


def _day_key(now: float) -> str:
    return "day:" + time.strftime("%Y-%m-%d", time.localtime(now))


def _hour_key(now: float) -> str:
    return "hour:" + time.strftime("%Y-%m-%dT%H", time.localtime(now))


def _shown(name: str, folder: str, roots: frozenset[str], folders: dict[str, bool]) -> bool:
    """False for a file search must not show: a blocked name, or a folder under a protected root.

    ``folders`` caches the answer per folder for one search, since hits share folders.
    """
    if file_refused(name):
        return False
    if folder not in folders:
        folders[folder] = not folder_refused(folder, roots)
    return folders[folder]


def _cap_bytes(text: str, limit: int) -> str:
    """The longest prefix of ``text`` whose UTF-8 form is at most ``limit`` bytes."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


def _like_escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
