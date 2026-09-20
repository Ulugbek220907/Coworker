"""Conversation memory that survives more than the last message.

Three layers, because a rolling window of recent turns is not enough:

  facts    - durable statements the model chose to remember ("shartnomalar
             D:/Ishxona/Shartnomalar ichida", "«zavod» = Tekstil zavodi").
             These outlive the conversation entirely.
  summary  - older turns compressed into prose once they fall out of the
             window, so nothing is silently dropped.
  window   - the last N verbatim turns.
  delivered- files actually sent, so "o'shani yana yubor" resolves.

Every layer is handed to the model on every request. That is what makes the
assistant answer from the whole relationship rather than the last sentence.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import config_dir

WINDOW_TURNS = 14          # verbatim turns kept in full
SUMMARY_TRIGGER = 22       # compress once the log grows past this
MAX_FACTS = 60
MAX_DELIVERED = 25


class ChatMemory:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id
        self.path = config_dir() / "chats" / f"{chat_id}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.turns: list[dict] = []
        self.facts: list[dict] = []
        self.summary: str = ""
        self.delivered: list[dict] = []
        self.load()

    # ------------------------------------------------------------ persistence

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self.turns = d.get("turns", [])
        self.facts = d.get("facts", [])
        self.summary = d.get("summary", "")
        self.delivered = d.get("delivered", [])

    def save(self) -> None:
        try:
            payload = {
                "turns": self.turns[-SUMMARY_TRIGGER * 2:],
                "facts": self.facts[-MAX_FACTS:],
                "summary": self.summary,
                "delivered": self.delivered[-MAX_DELIVERED:],
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def clear(self) -> None:
        """/forget - wipes the conversation but keeps durable facts."""
        self.turns = []
        self.summary = ""
        self.save()

    def forget_all(self) -> None:
        self.turns, self.facts, self.summary, self.delivered = [], [], "", []
        self.save()

    # ----------------------------------------------------------------- write

    def add_turn(self, role: str, content: str) -> None:
        if not content:
            return
        self.turns.append({"role": role, "content": content[:4000], "ts": time.time()})
        self.save()

    def remember(self, fact: str, kind: str = "note") -> str:
        """Store a durable fact, replacing a near-duplicate if one exists."""
        fact = fact.strip()
        if not fact:
            return "bo'sh"
        key = _key(fact)
        for existing in self.facts:
            if _key(existing["text"]) == key:
                existing["text"] = fact
                existing["ts"] = time.time()
                self.save()
                return "yangilandi"
        self.facts.append({"text": fact, "kind": kind, "ts": time.time()})
        if len(self.facts) > MAX_FACTS:
            self.facts = self.facts[-MAX_FACTS:]
        self.save()
        return "eslab qolindi"

    def forget_fact(self, needle: str) -> int:
        needle = needle.lower().strip()
        before = len(self.facts)
        self.facts = [f for f in self.facts if needle not in f["text"].lower()]
        self.save()
        return before - len(self.facts)

    def record_delivery(self, path: str, name: str) -> None:
        self.delivered = [d for d in self.delivered if d["path"] != path]
        self.delivered.append({"path": path, "name": name, "ts": time.time()})
        if len(self.delivered) > MAX_DELIVERED:
            self.delivered = self.delivered[-MAX_DELIVERED:]
        self.save()

    # ------------------------------------------------------------------ read

    def needs_summary(self) -> bool:
        return len(self.turns) > SUMMARY_TRIGGER

    def turns_to_compress(self) -> list[dict]:
        return self.turns[:-WINDOW_TURNS] if self.needs_summary() else []

    def apply_summary(self, new_summary: str) -> None:
        """Replace the compressed prefix with prose and drop those turns."""
        if not new_summary.strip():
            return
        self.summary = new_summary.strip()[:2500]
        self.turns = self.turns[-WINDOW_TURNS:]
        self.save()

    def context_block(self) -> str:
        """Everything the model should know before reading the new message."""
        parts: list[str] = []

        if self.facts:
            lines = [f"- {f['text']}" for f in self.facts[-30:]]
            parts.append("ESLAB QOLINGAN MA'LUMOTLAR:\n" + "\n".join(lines))

        if self.summary:
            parts.append("AVVALGI SUHBAT XULOSASI:\n" + self.summary)

        if self.delivered:
            recent = self.delivered[-8:]
            lines = [
                f"- {d['name']}  ({_ago(d['ts'])})  {d['path']}"
                for d in reversed(recent)
            ]
            parts.append("YAQINDA YUBORILGAN FAYLLAR:\n" + "\n".join(lines))

        return "\n\n".join(parts)

    def window(self) -> list[dict[str, Any]]:
        """Recent turns in OpenAI chat format."""
        return [
            {"role": t["role"], "content": t["content"]}
            for t in self.turns[-WINDOW_TURNS:]
            if t.get("content")
        ]


def _key(text: str) -> str:
    """Loose identity for a fact, so re-stating it updates instead of piling up."""
    from .textutil import tokens
    return " ".join(sorted(tokens(text))[:6])


def _ago(ts: float) -> str:
    delta = max(0, time.time() - ts)
    if delta < 3600:
        return f"{int(delta // 60)} daqiqa oldin"
    if delta < 86400:
        return f"{int(delta // 3600)} soat oldin"
    return f"{int(delta // 86400)} kun oldin"


class MemoryStore:
    """Lazy per-chat memory cache."""

    def __init__(self) -> None:
        self._cache: dict[int, ChatMemory] = {}

    def get(self, chat_id: int) -> ChatMemory:
        mem = self._cache.get(chat_id)
        if mem is None:
            mem = ChatMemory(chat_id)
            self._cache[chat_id] = mem
        return mem
