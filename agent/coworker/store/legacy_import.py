"""One-time import of the per-chat JSON memories written by the old memory.py.

Each ``chats/<chat_id>.json`` holds turns, a summary, facts and deliveries. The
import writes one chat per transaction, together with a marker row
``legacy_chat:<id>``, and only then renames the file to ``<name>.json.migrated``.
A crash at any point leaves either nothing imported or everything imported plus
the marker, so a second run never duplicates a chat.

Legacy facts carry no record of who said them, so they are imported as
untrusted: the model reads them as content, the safe default.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from ..config import config_dir
from .db import Store

log = logging.getLogger("store.legacy")

MIGRATED_SUFFIX = ".migrated"


def import_legacy_chats(store: Store, chats_dir: Path | None = None) -> int:
    """Import every legacy chat file not yet imported. Returns how many chats were imported."""
    folder = chats_dir if chats_dir is not None else config_dir() / "chats"
    if not folder.is_dir():
        return 0
    imported = 0
    for path in sorted(folder.glob("*.json")):
        try:
            chat_id = int(path.stem)
        except ValueError:
            continue  # not a chat file
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("legacy chat file could not be read and was left in place: %s", path.name)
            continue
        if not isinstance(payload, dict):
            log.warning("legacy chat file has an unexpected shape and was left in place: %s", path.name)
            continue
        marker = f"legacy_chat:{chat_id}"
        if store.kv_get(marker) is None:
            _import_chat(store, chat_id, payload, marker)
            imported += 1
        _retire(path)
    return imported


def _import_chat(store: Store, chat_id: int, payload: dict[str, Any], marker: str) -> None:
    now = time.time()
    with store.transaction() as conn:
        for turn in payload.get("turns") or []:
            if not isinstance(turn, dict) or not turn.get("content"):
                continue
            conn.execute(
                "INSERT INTO turns (chat_id, role, content, ts) VALUES (?, ?, ?, ?)",
                (chat_id, str(turn.get("role") or "user"), str(turn["content"]), _ts(turn, now)),
            )
        summary = str(payload.get("summary") or "").strip()
        if summary:
            conn.execute(
                "INSERT OR REPLACE INTO summaries (chat_id, text, updated_at) VALUES (?, ?, ?)",
                (chat_id, summary, now),
            )
        for fact in payload.get("facts") or []:
            if not isinstance(fact, dict) or not str(fact.get("text") or "").strip():
                continue
            conn.execute(
                "INSERT INTO facts (chat_id, text, kind, untrusted, ts) VALUES (?, ?, ?, 1, ?)",
                (chat_id, str(fact["text"]).strip(), str(fact.get("kind") or "note"), _ts(fact, now)),
            )
        for path, name, ts in _latest_deliveries(payload.get("delivered") or [], now):
            conn.execute(
                "INSERT INTO delivered (chat_id, path, name, ts) VALUES (?, ?, ?, ?)",
                (chat_id, path, name, ts),
            )
        conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (marker, json.dumps(now)))
    log.info("imported legacy chat %s", chat_id)


def _latest_deliveries(items: list[Any], now: float) -> list[tuple[str, str, float]]:
    """Each path once, at its newest position, in the order the old file kept them."""
    entries = [
        (str(item["path"]), str(item.get("name") or Path(str(item["path"])).name), _ts(item, now))
        for item in items
        if isinstance(item, dict) and item.get("path")
    ]
    last_index = {path: i for i, (path, _, _) in enumerate(entries)}
    return [entry for i, entry in enumerate(entries) if last_index[entry[0]] == i]


def _ts(item: dict[str, Any], default: float) -> float:
    try:
        return float(item.get("ts", default))
    except (TypeError, ValueError):
        return default


def _retire(path: Path) -> None:
    """Rename the source so it is not read again. The marker already protects the data."""
    try:
        os.replace(path, path.with_name(path.name + MIGRATED_SUFFIX))
    except OSError:
        log.warning("legacy chat file could not be renamed: %s", path.name)
