"""Pairing: the one-time code that binds the owner's Telegram account to this agent.

The code is shown only on the desktop or in the headless console, never sent
over Telegram. It is stored as a salted PBKDF2 hash, so the store file cannot
be used to pair a new chat. Each code allows five wrong guesses. Every wrong
guess, against any code, also counts toward a global lockout: twenty failures
within an hour stop all redemption until they age out. The failure list is
persisted, so restarting the agent does not reset the lockout.

A stranger can keep the lockout closed indefinitely: each time a failure ages
out, one more wrong guess re-arms it. The desktop therefore has ``clear_lockout``.
It is local only and must not run at startup, because a restart would then
reopen redemption for anyone who can make the agent restart.

The first successful redemption pins the owner's user id and chat id. Pinning
is final: re-binding is a local action, and no chat can ask for it.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Any

from ..store import Store

CODE_DIGITS = 8
CODE_TTL_S = 10 * 60
MAX_ATTEMPTS_PER_CODE = 5
GLOBAL_FAILURE_LIMIT = 20
GLOBAL_WINDOW_S = 60 * 60
PBKDF2_ITERATIONS = 200_000

CODE_KEY = "pair_code"
FAILURES_KEY = "pair_failures"
OWNER_USER_KEY = "owner_user_id"
OWNER_CHAT_KEY = "owner_chat_id"

MSG_PAIRED = "Bu bot allaqachon ulangan."
MSG_LOCKED = "Juda ko'p noto'g'ri urinish bo'ldi. Bir soatdan keyin qayta urinib ko'ring."
MSG_NOT_PRIVATE = "Ulash faqat shaxsiy chatda mumkin."
MSG_NO_CODE = "Kod eskirgan yoki mavjud emas. Kompyuterdagi yangi kodni kiriting."
MSG_WRONG = "Kod noto'g'ri."
MSG_OK = "Ulandi. Endi shu chat bilan ishlashingiz mumkin."


def _digest(code: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), salt, PBKDF2_ITERATIONS).hex()


def _usable(record: Any, now: float) -> bool:
    """A code can be redeemed while it is unexpired and has attempts left."""
    return record is not None and record["expires"] > now and record["attempts"] < MAX_ATTEMPTS_PER_CODE


class Pairing:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.Lock()

    def issue_code(self, now: float | None = None) -> str:
        """A new 8-digit code. It replaces any earlier code and is returned once, for display only."""
        moment = time.time() if now is None else now
        code = f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"
        salt = secrets.token_bytes(16)
        record = {
            "salt": salt.hex(),
            "hash": _digest(code, salt),
            "expires": moment + CODE_TTL_S,
            "attempts": 0,
        }
        with self._lock:
            self._store.kv_set(CODE_KEY, record)
        return code

    def redeem(
        self,
        code: str,
        from_id: int,
        chat_id: int,
        chat_type: str,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """Try a code from a chat. Returns (paired, message for the chat)."""
        moment = time.time() if now is None else now
        with self._lock:
            if self.owner() is not None:
                return False, MSG_PAIRED
            if len(self._recent_failures(moment)) >= GLOBAL_FAILURE_LIMIT:
                return False, MSG_LOCKED
            if chat_type != "private":
                return False, MSG_NOT_PRIVATE
            record = self._store.kv_get(CODE_KEY)
            if not _usable(record, moment):
                self._record_failure(moment)
                return False, MSG_NO_CODE
            salt = bytes.fromhex(record["salt"])
            candidate = _digest(str(code).strip(), salt)
            if not hmac.compare_digest(candidate.encode("utf-8"), record["hash"].encode("utf-8")):
                record["attempts"] += 1
                self._store.kv_set(CODE_KEY, record)
                self._record_failure(moment)
                return False, MSG_WRONG
            self._store.kv_set(OWNER_USER_KEY, int(from_id))
            self._store.kv_set(OWNER_CHAT_KEY, int(chat_id))
            self._store.kv_set(CODE_KEY, None)  # single use, whatever happens next
            return True, MSG_OK

    def clear_lockout(self) -> None:
        """Forget the recent failures so redemption opens again. Call only from a local action.

        Nothing reachable from a chat calls this, and the startup path does not
        either: issue_code runs at every start, so clearing there would make a
        restart a way around the lockout.
        """
        with self._lock:
            self._store.kv_set(FAILURES_KEY, [])

    def owner(self) -> tuple[int, int] | None:
        """The pinned (user id, chat id) of the owner, or None before pairing."""
        user = self._store.kv_get(OWNER_USER_KEY)
        chat = self._store.kv_get(OWNER_CHAT_KEY)
        if user is None or chat is None:
            return None
        return int(user), int(chat)

    def _recent_failures(self, now: float) -> list[float]:
        stored = self._store.kv_get(FAILURES_KEY, [])
        return [t for t in stored if t > now - GLOBAL_WINDOW_S]

    def _record_failure(self, now: float) -> None:
        self._store.kv_set(FAILURES_KEY, self._recent_failures(now) + [now])
