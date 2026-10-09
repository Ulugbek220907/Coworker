"""Forward-only schema migrations for the Coworker store.

Each migration is a list of single SQL statements. Every pending migration runs
inside the caller's IMMEDIATE transaction together with its row in
``schema_version``, so a crash leaves the database either before or after a
step, never in between. There are no down migrations: a mistake is repaired by
a new migration, never by editing one that has already been applied.

Full-text search is created only when the SQLite build has FTS5. Facts and notes
keep working without it through a scan in the store, and the table's presence
records which mode the database was created in, so no code ever asks for a
module that is not there.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Callable

Statements = list[str]

_CORE: Statements = [
    "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)",

    "CREATE TABLE turns ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " chat_id INTEGER NOT NULL,"
    " role TEXT NOT NULL,"
    " content TEXT NOT NULL,"
    " ts REAL NOT NULL,"
    " hidden INTEGER NOT NULL DEFAULT 0)",
    "CREATE INDEX turns_chat ON turns (chat_id, id)",

    "CREATE TABLE summaries ("
    " chat_id INTEGER PRIMARY KEY,"
    " text TEXT NOT NULL,"
    " updated_at REAL NOT NULL)",

    "CREATE TABLE facts ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " chat_id INTEGER NOT NULL,"
    " text TEXT NOT NULL,"
    " kind TEXT NOT NULL,"
    " untrusted INTEGER NOT NULL DEFAULT 0,"
    " ts REAL NOT NULL,"
    " hidden INTEGER NOT NULL DEFAULT 0)",
    "CREATE INDEX facts_chat ON facts (chat_id, id)",

    "CREATE TABLE delivered ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " chat_id INTEGER NOT NULL,"
    " path TEXT NOT NULL,"
    " name TEXT NOT NULL,"
    " ts REAL NOT NULL,"
    " hidden INTEGER NOT NULL DEFAULT 0)",
    "CREATE INDEX delivered_chat ON delivered (chat_id, id)",

    "CREATE TABLE approvals ("
    " id TEXT PRIMARY KEY,"
    " nonce TEXT NOT NULL,"
    " chat_id INTEGER NOT NULL,"
    " tool TEXT NOT NULL,"
    " args TEXT NOT NULL,"
    " summary TEXT NOT NULL,"
    " provenance INTEGER NOT NULL,"
    " autonomy TEXT NOT NULL,"
    " generation INTEGER NOT NULL,"
    " status TEXT NOT NULL,"
    " two_channel INTEGER NOT NULL DEFAULT 0,"
    " local_ok INTEGER NOT NULL DEFAULT 0,"
    " created_at REAL NOT NULL,"
    " expires_at REAL NOT NULL,"
    " note TEXT NOT NULL DEFAULT '')",
    "CREATE INDEX approvals_status ON approvals (status, chat_id)",

    # Audit rows. Outcome columns are the only ones that may change, once.
    "CREATE TABLE audit ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " turn_id TEXT NOT NULL,"
    " actor TEXT NOT NULL,"
    " tool TEXT NOT NULL,"
    " tier TEXT NOT NULL,"
    " decision TEXT NOT NULL,"
    " code TEXT NOT NULL,"
    " args_summary TEXT NOT NULL,"
    " provider TEXT NOT NULL,"
    " ts REAL NOT NULL,"
    " prev_hash TEXT NOT NULL,"
    " hash TEXT NOT NULL,"
    " tag TEXT NOT NULL,"
    " outcome_ok INTEGER,"
    " outcome_code TEXT,"
    " outcome_summary TEXT,"
    " outcome_ts REAL,"
    " outcome_tag TEXT)",
    "CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit"
    " BEGIN SELECT RAISE(ABORT, 'audit rows are append-only'); END",
    "CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit"
    " WHEN OLD.outcome_ts IS NOT NULL OR NEW.outcome_ts IS NULL"
    " OR NEW.id IS NOT OLD.id OR NEW.turn_id IS NOT OLD.turn_id"
    " OR NEW.actor IS NOT OLD.actor OR NEW.tool IS NOT OLD.tool"
    " OR NEW.tier IS NOT OLD.tier OR NEW.decision IS NOT OLD.decision"
    " OR NEW.code IS NOT OLD.code OR NEW.args_summary IS NOT OLD.args_summary"
    " OR NEW.provider IS NOT OLD.provider OR NEW.ts IS NOT OLD.ts"
    " OR NEW.prev_hash IS NOT OLD.prev_hash OR NEW.hash IS NOT OLD.hash"
    " OR NEW.tag IS NOT OLD.tag"
    " BEGIN SELECT RAISE(ABORT, 'audit rows are append-only; only an outcome may be set, once'); END",

    "CREATE TABLE reminders ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " chat_id INTEGER NOT NULL,"
    " text TEXT NOT NULL,"
    " due_ts REAL NOT NULL,"
    " repeat TEXT,"
    " status TEXT NOT NULL DEFAULT 'active',"
    " created_at REAL NOT NULL)",
    "CREATE INDEX reminders_due ON reminders (status, due_ts)",

    "CREATE TABLE jobs ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " chat_id INTEGER NOT NULL,"
    " name TEXT NOT NULL,"
    " instruction TEXT NOT NULL,"
    " schedule TEXT NOT NULL,"
    " next_run_ts REAL NOT NULL,"
    " enabled INTEGER NOT NULL DEFAULT 1,"
    " paused INTEGER NOT NULL DEFAULT 0,"
    " failures INTEGER NOT NULL DEFAULT 0,"
    " last_run_ts REAL,"
    " last_status TEXT NOT NULL DEFAULT '',"
    " created_at REAL NOT NULL)",
    "CREATE INDEX jobs_due ON jobs (enabled, paused, next_run_ts)",

    "CREATE TABLE notes ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " chat_id INTEGER NOT NULL,"
    " title TEXT NOT NULL,"
    " body TEXT NOT NULL,"
    " ts REAL NOT NULL)",
    "CREATE INDEX notes_chat ON notes (chat_id, id)",

    "CREATE TABLE tasks ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " chat_id INTEGER NOT NULL,"
    " text TEXT NOT NULL,"
    " due_ts REAL,"
    " done INTEGER NOT NULL DEFAULT 0,"
    " created_at REAL NOT NULL)",
    "CREATE INDEX tasks_chat ON tasks (chat_id, done, id)",
]


def _search_statements(conn: sqlite3.Connection) -> Statements:
    """FTS5 indexes for facts and notes, kept in step by triggers.

    Returns nothing when FTS5 is missing; the store then searches by scanning.
    """
    if not fts5_available(conn):
        return []
    return [
        "CREATE VIRTUAL TABLE facts_fts USING fts5(text, content='facts', content_rowid='id')",
        "CREATE TRIGGER facts_fts_ai AFTER INSERT ON facts BEGIN"
        " INSERT INTO facts_fts (rowid, text) VALUES (new.id, new.text); END",
        "CREATE TRIGGER facts_fts_ad AFTER DELETE ON facts BEGIN"
        " INSERT INTO facts_fts (facts_fts, rowid, text) VALUES ('delete', old.id, old.text); END",
        "CREATE TRIGGER facts_fts_au AFTER UPDATE OF text ON facts BEGIN"
        " INSERT INTO facts_fts (facts_fts, rowid, text) VALUES ('delete', old.id, old.text);"
        " INSERT INTO facts_fts (rowid, text) VALUES (new.id, new.text); END",
        "INSERT INTO facts_fts (rowid, text) SELECT id, text FROM facts",

        "CREATE VIRTUAL TABLE notes_fts USING fts5(title, body, content='notes', content_rowid='id')",
        "CREATE TRIGGER notes_fts_ai AFTER INSERT ON notes BEGIN"
        " INSERT INTO notes_fts (rowid, title, body) VALUES (new.id, new.title, new.body); END",
        "CREATE TRIGGER notes_fts_ad AFTER DELETE ON notes BEGIN"
        " INSERT INTO notes_fts (notes_fts, rowid, title, body)"
        " VALUES ('delete', old.id, old.title, old.body); END",
        "CREATE TRIGGER notes_fts_au AFTER UPDATE OF title, body ON notes BEGIN"
        " INSERT INTO notes_fts (notes_fts, rowid, title, body)"
        " VALUES ('delete', old.id, old.title, old.body);"
        " INSERT INTO notes_fts (rowid, title, body) VALUES (new.id, new.title, new.body); END",
        "INSERT INTO notes_fts (rowid, title, body) SELECT id, title, body FROM notes",
    ]


MIGRATIONS: tuple[tuple[int, str, Callable[[sqlite3.Connection], Statements]], ...] = (
    (1, "core tables and audit triggers", lambda conn: _CORE),
    (2, "full-text search for facts and notes", _search_statements),
)


def fts5_available(conn: sqlite3.Connection) -> bool:
    """True when this SQLite build can create FTS5 tables."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(x)")
    except sqlite3.OperationalError:
        return False
    conn.execute("DROP TABLE temp.fts5_probe")
    return True


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return row is not None


def apply(conn: sqlite3.Connection) -> list[int]:
    """Run every migration not yet recorded. The caller holds the write transaction."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL)"
    )
    done = {row[0] for row in conn.execute("SELECT version FROM schema_version")}
    applied: list[int] = []
    for version, name, build in MIGRATIONS:
        if version in done:
            continue
        for statement in build(conn):
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, time.time()),
        )
        applied.append(version)
    return applied
