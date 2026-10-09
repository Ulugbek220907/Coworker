"""Connection handling shared by every store table.

SQLite serialises writers, and the store is called from the poller, the
scheduler, governor threads and the desktop UI at once. Three rules keep that
safe:

* each thread gets its own connection, so a connection is never used by two
  threads at the same time;
* every write runs inside ``transaction()``, which holds a process-local lock
  and opens an IMMEDIATE transaction, so a writer never acts on a stale read;
* a 5 second busy timeout lets another process finish its write instead of
  failing with "database is locked".

WAL mode lets readers keep going while a writer holds the lock.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import migrations

BUSY_TIMEOUT_S = 5.0

# kv key of the kill-switch generation. Approval consumption reads it inside the
# same transaction that flips the approval, so a stop and a tap cannot interleave.
KILL_GENERATION_KEY = "kill_generation"


class StoreError(RuntimeError):
    """A store operation could not be completed. The database is left unchanged."""


def row_dict(
    row: sqlite3.Row,
    *,
    json_keys: tuple[str, ...] = (),
    flag_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    """A row as a plain dict, with JSON columns decoded and 0/1 columns made bool."""
    out = dict(row)
    for key in json_keys:
        if out.get(key) is not None:
            out[key] = json.loads(out[key])
    for key in flag_keys:
        if out.get(key) is not None:
            out[key] = bool(out[key])
    return out


class StoreBase:
    """Connections, transactions and migrations. Table mixins build on this."""

    def __init__(self, path: Path | str, *, use_fts: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = False
        # The audit HMAC key, once read from the keyring. Kept in memory only.
        self._audit_secret: str | None = None
        with self.transaction() as conn:
            migrations.apply(conn)
            self.fts_enabled = use_fts and migrations.has_table(conn, "facts_fts")

    def close(self) -> None:
        """Close every thread's connection. The store cannot be used afterwards."""
        with self._connections_lock:
            connections, self._connections = self._connections, []
            self._closed = True
        for conn in connections:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        if self._closed:
            raise StoreError("the store is closed")
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.path,
                timeout=BUSY_TIMEOUT_S,
                isolation_level=None,  # explicit BEGIN/COMMIT only
                check_same_thread=False,  # close() may run on another thread
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            with self._connections_lock:
                if self._closed:
                    conn.close()
                    raise StoreError("the store is closed")
                self._connections.append(conn)
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One IMMEDIATE transaction: committed on success, rolled back on any error.

        Do not nest calls. The write lock is not re-entrant.
        """
        conn = self._conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _read(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self._conn().execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self._conn().execute(sql, params).fetchone()
