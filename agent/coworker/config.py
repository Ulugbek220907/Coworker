"""Agent configuration, persisted next to the user's other app data.

The config file is the agent's entire "database": which chats are trusted,
which folders have proved useful, and where the LLM lives. Losing it costs
one re-pairing, nothing more.
"""
from __future__ import annotations

import json
import os
import platform
import secrets
import uuid
from pathlib import Path
from typing import Any

APP_NAME = "Coworker"

# Files that should never leave the machine, whatever the model decides.
# Matched case-insensitively as substrings of the filename.
DEFAULT_BLOCKED = [
    "password", "parol", "parool", "api key", "api_key", "apikey",
    "secret", "token", "wallet", "seed phrase", "private key", "id_rsa",
    ".env", ".pem", ".ppk", ".kdbx", ".keystore", "credential",
]

DEFAULTS: dict[str, Any] = {
    "agent_id": "",
    "name": "",
    "server_url": "http://127.0.0.1:8000",
    "relay_token": "",

    # LLM - any OpenAI-compatible endpoint works (DeepSeek, GLM, Ollama, ...).
    "llm_base_url": "https://api.deepseek.com",
    "llm_api_key": "",
    "llm_model": "deepseek-chat",

    # Search scope. Empty roots means "every fixed drive".
    "roots": [],
    "priority_dirs": [],
    "blocked_patterns": DEFAULT_BLOCKED,
    "max_file_mb": 45,

    # Behaviour
    "autostart": False,
    "auto_send_single_hit": True,   # one confident match -> just send it
    "reply_language": "auto",       # auto | uz | ru
    "max_reply_chars": 600,

    # Speech to text
    "stt_enabled": True,
    "stt_engine": "auto",           # auto | faster-whisper | vosk | off
    "stt_model": "base",            # faster-whisper size, or a vosk model name

    # Trusted chats, learned through pairing.
    "chats": [],
}


def config_dir() -> Path:
    if os.name == "nt":
        base = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


class Config:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (config_dir() / "config.json")
        self.data: dict[str, Any] = dict(DEFAULTS)
        self.load()
        # A fresh install needs a stable identity and a one-time pair code.
        if not self.data.get("agent_id"):
            self.data["agent_id"] = uuid.uuid4().hex
        if not self.data.get("name"):
            self.data["name"] = platform.node() or "PC"
        self.pair_code = f"{secrets.randbelow(900000) + 100000}"
        self.save()

    # ------------------------------------------------------------ persistence

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                self.data.update(stored)
        except (json.JSONDecodeError, OSError):
            pass  # a corrupt config should not stop the app from starting

    def save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
        except OSError:
            pass

    # --------------------------------------------------------------- access

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    # ----------------------------------------------------------- trust list

    @property
    def chats(self) -> list[int]:
        return [int(c) for c in self.data.get("chats", [])]

    def authorize(self, chat_id: int) -> bool:
        """Returns True if this is a newly trusted chat."""
        chats = self.chats
        if chat_id in chats:
            return False
        chats.append(chat_id)
        self.set("chats", chats)
        return True

    def revoke(self, chat_id: int) -> None:
        self.set("chats", [c for c in self.chats if c != chat_id])

    # --------------------------------------------------------- search scope

    def search_roots(self) -> list[str]:
        roots = [r for r in self.data.get("roots", []) if os.path.isdir(r)]
        if roots:
            return roots
        from .fs import list_drives
        return [d["path"] for d in list_drives()]

    def remember_dir(self, path: str) -> None:
        """Promote a folder that just produced a useful answer."""
        if not path or not os.path.isdir(path):
            return
        dirs = [d for d in self.data.get("priority_dirs", []) if d != path]
        dirs.insert(0, path)
        self.set("priority_dirs", dirs[:12])

    # -------------------------------------------------------------- safety

    def is_blocked(self, path: str) -> bool:
        """Sensitive files stay on the machine even if the model asks."""
        name = os.path.basename(path).lower()
        full = str(path).lower()
        for pat in self.data.get("blocked_patterns", DEFAULT_BLOCKED):
            pat = pat.lower().strip()
            if not pat:
                continue
            if pat.startswith(".") and full.endswith(pat):
                return True
            if pat in name:
                return True
        return False

    @property
    def max_file_bytes(self) -> int:
        return int(self.get("max_file_mb", 45)) * 1024 * 1024

    @property
    def ws_url(self) -> str:
        base = str(self.get("server_url", "")).rstrip("/")
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):] + "/ws/agent"
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):] + "/ws/agent"
        return base + "/ws/agent"

    @property
    def configured(self) -> bool:
        return bool(self.get("llm_api_key")) and bool(self.get("server_url"))
