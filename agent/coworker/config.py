"""Agent configuration, persisted next to the user's other app data.

The config file is the agent's entire "database": which chats are trusted,
which folders have proved useful, and where the LLM lives. Losing it costs
one re-pairing, nothing more.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import uuid
from pathlib import Path
from typing import Any

from .store.secrets import SecretStoreError, get_secret, set_secret

log = logging.getLogger("config")

APP_NAME = "Coworker"

# Credentials never reach config.json. They live in the OS keyring (see
# store/secrets.py), and Config.save() drops them even if they are in memory.
SECRET_KEYS = ("llm_api_key", "relay_token")

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

    # LLM - any OpenAI-compatible endpoint works (DeepSeek, GLM, Ollama, ...).
    # The API key itself is a secret; see SECRET_KEYS.
    "llm_base_url": "https://api.deepseek.com",
    "llm_model": "deepseek-chat",

    # Search scope. Empty roots means "every fixed drive".
    "roots": [],
    "priority_dirs": [],
    "blocked_patterns": DEFAULT_BLOCKED,
    "max_file_mb": 45,

    # Behaviour
    "autostart": False,
    # ask_always | ask_for_writes | autonomous_readonly  (see core.types.Autonomy)
    "autonomy": "ask_for_writes",
    # Tool families the owner has switched off, on top of the grants above.
    "disabled_families": [],
    # File deletion stays off until the owner has checked the Recycle Bin path on this PC.
    "recycle_verified": False,
    # Where generated files go. Empty means COWORKER_HOME/scratch; see Config.scratch_dir.
    "scratch_dir": "",
    "auto_send_single_hit": True,   # one confident match -> just send it
    "reply_language": "auto",       # auto | uz | ru
    "max_reply_chars": 600,

    # Browser. Headless hides the window; visible is the default so the
    # user can sign in to sites once and watch what the agent does.
    "browser_headless": False,
    # "profile" = agent's own Chrome profile (sign in once).
    # "cdp"     = attach to the user's real Chrome with their accounts.
    "browser_mode": "profile",
    "browser_cdp_port": 9222,
    "chrome_profile": "Default",
    # Vision model for reading screens (screen_read). deepseek-flash sees
    # images well; it is only used when a screen is explicitly read.
    "vision_model": "deepseek-flash",

    # Speech to text
    "stt_enabled": True,
    "stt_engine": "auto",           # auto | faster-whisper | vosk | off
    "stt_model": "base",            # faster-whisper size, or a vosk model name

    # Trusted chats, learned through pairing.
    "chats": [],

    # What each chat is allowed to do, keyed by chat id. A newly paired chat
    # gets FIND only: the father's phone must never reach the tools that can
    # change a file, and capability is granted deliberately in the UI rather
    # than inherited by being trusted at all.
    "chat_caps": {},
    "default_caps": ["find"],
}

# Capability names, narrowest first.
CAP_FIND = "find"              # search the disk, send a file back
CAP_OFFICE = "office"          # read spreadsheets, convert to PDF, split PDFs
CAP_OFFICE_WRITE = "office_write"   # modify a document - always confirmed
CAP_DESKTOP = "desktop"             # read open windows as a text tree
CAP_DESKTOP_CONTROL = "desktop_control"   # click and type into them
CAP_BROWSER = "browser"             # open and drive web pages
CAP_SYSTEM = "system"               # master volume and window state

ALL_CAPS = (
    CAP_FIND, CAP_OFFICE, CAP_OFFICE_WRITE,
    CAP_DESKTOP, CAP_DESKTOP_CONTROL, CAP_BROWSER, CAP_SYSTEM,
)

CAP_LABELS = {
    CAP_FIND: "Hujjat topish va yuborish",
    CAP_OFFICE: "Jadvallarni o'qish, PDF'ga o'girish",
    CAP_OFFICE_WRITE: "Fayllarni o'zgartirish (tasdiq bilan)",
    CAP_DESKTOP: "Ochiq oynalarni ko'rish",
    CAP_DESKTOP_CONTROL: "Oynalarni boshqarish (bosish, yozish)",
    CAP_BROWSER: "Brauzer: sayt ochish va to'ldirish",
    CAP_SYSTEM: "Ovoz va oyna holatini boshqarish",
}


def config_dir() -> Path:
    """The folder for config, store, chats and scratch files.

    COWORKER_HOME overrides the per-user location; tests and portable installs use it.
    """
    override = os.getenv("COWORKER_HOME", "").strip()
    if override:
        d = Path(override)
    else:
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
        # Secrets the keyring refused to take. Held for this run only; never written.
        self._session_secrets: dict[str, str] = {}
        self.load()
        # A fresh install needs a stable identity. Pairing codes live in the store.
        if not self.data.get("agent_id"):
            self.data["agent_id"] = uuid.uuid4().hex
        if not self.data.get("name"):
            self.data["name"] = platform.node() or "PC"
        self.save()

    # ------------------------------------------------------------ persistence

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return  # a corrupt config should not stop the app from starting
        if not isinstance(stored, dict):
            return
        # Secrets written by an older version move to the keyring now, and the
        # file is rewritten without them.
        moved = False
        for key in SECRET_KEYS:
            value = stored.pop(key, "")
            if value:
                self._store_secret(key, str(value))
                moved = True
        self.data.update(stored)
        if moved:
            self.save()

    def save(self) -> None:
        payload = {key: value for key, value in self.data.items() if key not in SECRET_KEYS}
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
        except OSError:
            pass

    # --------------------------------------------------------------- access

    def get(self, key: str, default: Any = None) -> Any:
        if key in SECRET_KEYS:
            return self._secret(key)
        return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        if key in SECRET_KEYS:
            self._store_secret(key, str(value or ""))
            return
        self.data[key] = value
        self.save()

    def _secret(self, key: str) -> str:
        return self._session_secrets.get(key) or get_secret(key) or ""

    def _store_secret(self, key: str, value: str) -> None:
        try:
            set_secret(key, value)
        except SecretStoreError:
            log.warning("no keyring is available; %s is kept for this run only", key)
            self._session_secrets[key] = value
            return
        self._session_secrets.pop(key, None)

    @property
    def scratch_dir(self) -> Path:
        """Folder for generated files such as PDF conversions. Created on first use."""
        override = str(self.get("scratch_dir", "") or "")
        d = Path(override) if override else config_dir() / "scratch"
        d.mkdir(parents=True, exist_ok=True)
        return d

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
        caps = dict(self.data.get("chat_caps", {}))
        caps.pop(str(chat_id), None)
        self.set("chat_caps", caps)

    # --------------------------------------------------------- capabilities

    def caps(self, chat_id: int) -> list[str]:
        stored = self.data.get("chat_caps", {}).get(str(chat_id))
        if stored is None:
            return list(self.get("default_caps", [CAP_FIND]))
        return [c for c in stored if c in ALL_CAPS]

    def set_caps(self, chat_id: int, caps: list[str]) -> None:
        table = dict(self.data.get("chat_caps", {}))
        # FIND is what pairing means; there is no useful chat without it.
        table[str(chat_id)] = sorted(
            {CAP_FIND, *(c for c in caps if c in ALL_CAPS)},
            key=ALL_CAPS.index,
        )
        self.set("chat_caps", table)

    def allows(self, chat_id: int, capability: str) -> bool:
        return capability in self.caps(chat_id)

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
