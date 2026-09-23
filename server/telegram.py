"""Thin async wrapper over the Telegram Bot API.

Deliberately dependency-light: just httpx. The relay never inspects message
content beyond routing, so there is no need for a full bot framework here.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger("tg")

# Overridable so integration tests can point at a local mock.
API = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org")
# Telegram hard limit is 4096 UTF-16 code units; stay well under it.
CHUNK = 3500


class Telegram:
    def __init__(self, token: str) -> None:
        self.token = token
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))

    @property
    def base(self) -> str:
        return f"{API}/bot{self.token}"

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, **params: Any) -> dict:
        """Always returns a dict. A gateway 502, an HTML error page or a
        dropped connection must not take down the routing task that called us."""
        try:
            r = await self._client.post(f"{self.base}/{method}", json=params)
        except httpx.HTTPError as exc:
            log.warning("telegram %s unreachable: %s", method, exc)
            return {"ok": False, "description": f"network error: {exc}"}

        try:
            data = r.json()
        except ValueError:
            log.warning("telegram %s returned non-JSON (%s): %s",
                        method, r.status_code, r.text[:200])
            return {"ok": False, "description": f"HTTP {r.status_code}: {r.text[:200]}"}

        if not isinstance(data, dict):
            return {"ok": False, "description": f"unexpected payload: {str(data)[:200]}"}
        if not data.get("ok"):
            log.warning("telegram %s failed: %s", method, data.get("description"))
        return data

    async def send_text(
        self,
        chat_id: int,
        text: str,
        buttons: list[list[dict]] | None = None,
        reply_to: int | None = None,
    ) -> dict:
        """Send text, splitting on paragraph boundaries when it is too long."""
        parts = _split(text or "\u00b7")
        result: dict = {}
        for i, part in enumerate(parts):
            params: dict[str, Any] = {"chat_id": chat_id, "text": part}
            # Keyboard and reply-to only belong on the final chunk.
            if i == len(parts) - 1:
                if buttons:
                    params["reply_markup"] = {"inline_keyboard": buttons}
                if reply_to:
                    params["reply_to_message_id"] = reply_to
                    params["allow_sending_without_reply"] = True
            result = await self.call("sendMessage", **params)
        return result

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        await self.call("sendChatAction", chat_id=chat_id, action=action)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    async def edit_reply_markup(self, chat_id: int, message_id: int) -> None:
        """Strip buttons from a message once its choice has been consumed."""
        await self.call(
            "editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
            reply_markup={"inline_keyboard": []},
        )

    async def get_file_bytes(self, file_id: str, max_bytes: int) -> bytes | None:
        info = await self.call("getFile", file_id=file_id)
        if not info.get("ok"):
            return None
        path = info["result"].get("file_path")
        size = info["result"].get("file_size") or 0
        if not path or size > max_bytes:
            return None
        r = await self._client.get(f"{API}/file/bot{self.token}/{path}")
        if r.status_code != 200:
            return None
        return r.content

    async def send_document(
        self, chat_id: int, filename: str, content: bytes, caption: str = ""
    ) -> dict:
        files = {"document": (filename, content, "application/octet-stream")}
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption[:1000]
        r = await self._client.post(f"{self.base}/sendDocument", data=data, files=files)
        try:
            return r.json()
        except Exception:
            return {"ok": False, "description": r.text[:200]}

    async def send_photo(
        self, chat_id: int, filename: str, content: bytes, caption: str = ""
    ) -> dict:
        """Images go as photos so they preview inline on the phone rather than
        arriving as a file to download - screenshots and scans especially."""
        files = {"photo": (filename, content, "application/octet-stream")}
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption[:1000]
        r = await self._client.post(f"{self.base}/sendPhoto", data=data, files=files)
        try:
            out = r.json()
        except Exception:
            return {"ok": False, "description": r.text[:200]}
        # Telegram rejects oversized or odd-ratio images as photos; fall back
        # to a document so the user still gets the file.
        if not out.get("ok"):
            return await self.send_document(chat_id, filename, content, caption)
        return out

    async def set_webhook(self, url: str, secret: str) -> dict:
        return await self.call(
            "setWebhook",
            url=url,
            secret_token=secret,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )

    async def set_commands(self) -> dict:
        return await self.call(
            "setMyCommands",
            commands=[
                {"command": "start", "description": "Boshlash / Начало"},
                {"command": "connect", "description": "Kompyuterga ulanish (kod bilan)"},
                {"command": "status", "description": "Ulanish holati"},
                {"command": "forget", "description": "Suhbat tarixini tozalash"},
                {"command": "help", "description": "Yordam"},
            ],
        )


def _split(text: str) -> list[str]:
    """Split long text on the nicest available boundary."""
    if len(text) <= CHUNK:
        return [text]
    parts, buf = [], ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > CHUNK:
            if buf:
                parts.append(buf)
                buf = ""
            # A single line longer than the limit must still be cut hard.
            while len(line) > CHUNK:
                parts.append(line[:CHUNK])
                line = line[CHUNK:]
        buf += line
    if buf:
        parts.append(buf)
    return parts
