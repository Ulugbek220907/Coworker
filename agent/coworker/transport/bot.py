"""Synchronous client for the Telegram Bot API that reports failures as data.

Each method makes one request and returns the API's JSON object as a dict. A
network or HTTP failure never raises: it comes back as ``{"ok": False, ...}``
with ``error_code`` set to the HTTP status, or to None when no response
arrived at all. The polling loop depends on that split: 409 means another
consumer owns the bot and polling must stop, while anything else is worth a
pause and a retry.

Retries happen only when no response arrived. An HTTP error is Telegram's
answer, and asking again would only repeat it. The known cost: a timeout after
Telegram accepted a sendMessage can duplicate that message on retry. That is
accepted, because a lost reply on a flaky link is the worse outcome.

The bot token is part of every request URL, and httpx logs each URL at INFO.
TokenRedactor scrubs those records, so the token never reaches a log file.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import re
import time
from typing import Any, Callable

import httpx

log = logging.getLogger("transport.bot")

API_BASE = "https://api.telegram.org"
LONG_POLL_S = 25
POLL_HTTP_MARGIN_S = 10.0
UPLOAD_TIMEOUT_S = 120.0
DEFAULT_DOWNLOAD_BYTES = 20 * 1024 * 1024
CAPTION_LIMIT = 1000
NETWORK_PAUSES_S = (1.0, 2.0)  # one pause before each retry; three attempts in all

# Telegram puts the token in the URL as bot<id>:<secret>. A bare token can
# also appear in an exception message, so both shapes are scrubbed.
_BOT_PATH = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_BARE_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}")


def redact(text: str) -> str:
    text = _BOT_PATH.sub("bot<redacted>", text)
    return _BARE_TOKEN.sub("<redacted>", text)


class TokenRedactor(logging.Filter):
    """Rewrites each record to its redacted text, traceback included.

    The message is formatted here, before the arguments are dropped, so a
    token passed as a format argument is caught too.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a bad format string must not become a leak
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        return True


# httpx and httpcore log every request URL; transport.bot logs exception text.
_REDACTED_LOGGERS = ("httpx", "httpcore", "transport.bot")


def install_redaction() -> None:
    """Attach TokenRedactor to the loggers that can see a token. Idempotent."""
    for name in _REDACTED_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, TokenRedactor) for f in logger.filters):
            logger.addFilter(TokenRedactor())


def _failure(description: str, error_code: int | None = None) -> dict:
    return {"ok": False, "error_code": error_code, "description": description}


def _as_dict(label: str, resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        # A gateway error page is HTML. Its body is not echoed: it can be large.
        log.warning("telegram %s returned non-JSON (HTTP %s)", label, resp.status_code)
        return _failure(f"HTTP {resp.status_code}", resp.status_code)
    if not isinstance(data, dict):
        return _failure("unexpected payload", resp.status_code)
    if not data.get("ok"):
        data.setdefault("error_code", resp.status_code)
        log.warning("telegram %s failed (%s): %s", label, data.get("error_code"), data.get("description"))
    return data


def _fields(chat_id: int, caption: str) -> dict[str, str]:
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption[:CAPTION_LIMIT]
    return fields


class BotApi:
    """One bot, one token. The token is never stored outside the URL."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token:
            raise ValueError("a bot token is required")
        self._base = f"{API_BASE}/bot{token}"
        self._file_base = f"{API_BASE}/file/bot{token}"
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._sleep = sleep
        install_redaction()

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------ transport

    def _exchange(self, label: str, send: Callable[[], httpx.Response]) -> httpx.Response | dict:
        """Run one HTTP exchange. Only a missing response is retried."""
        last_error = ""
        for attempt in range(len(NETWORK_PAUSES_S) + 1):
            try:
                return send()
            except httpx.TransportError as exc:
                last_error = redact(str(exc)) or type(exc).__name__
                log.warning("telegram %s: network error, attempt %d: %s", label, attempt + 1, last_error)
                if attempt < len(NETWORK_PAUSES_S):
                    self._sleep(NETWORK_PAUSES_S[attempt])
            except (httpx.HTTPError, OSError) as exc:
                log.warning("telegram %s failed: %s", label, redact(str(exc)) or type(exc).__name__)
                return _failure(f"request failed: {type(exc).__name__}")
        return _failure(f"network error: {last_error}")

    def _call(self, method: str, body: dict | None = None, *, timeout: float = 30.0) -> dict:
        url = f"{self._base}/{method}"
        resp = self._exchange(method, lambda: self._client.post(url, json=body or {}, timeout=timeout))
        return resp if isinstance(resp, dict) else _as_dict(method, resp)

    def _upload(self, method: str, field: str, path: str, fields: dict[str, str]) -> dict:
        url = f"{self._base}/{method}"
        name = os.path.basename(path)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"

        def send() -> httpx.Response:
            with open(path, "rb") as fh:
                return self._client.post(
                    url, data=fields, files={field: (name, fh, mime)}, timeout=UPLOAD_TIMEOUT_S,
                )

        resp = self._exchange(method, send)
        return resp if isinstance(resp, dict) else _as_dict(method, resp)

    # -------------------------------------------------------------- updates

    def get_updates(self, offset: int, timeout: int = LONG_POLL_S) -> dict:
        body: dict[str, Any] = {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        return self._call("getUpdates", body, timeout=timeout + POLL_HTTP_MARGIN_S)

    def delete_webhook(self) -> dict:
        """Polling and a webhook cannot coexist; Telegram answers 409 if both are set."""
        return self._call("deleteWebhook", {"drop_pending_updates": False})

    def set_my_commands(self, commands: list[dict]) -> dict:
        return self._call("setMyCommands", {"commands": commands})

    # ------------------------------------------------------------- messages

    def send_message(
        self,
        chat_id: int,
        text: str,
        buttons: list[list[dict]] | None = None,
        reply_to: int | None = None,
    ) -> dict:
        body: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": buttons}
        if reply_to is not None:
            body["reply_to_message_id"] = reply_to
            body["allow_sending_without_reply"] = True
        return self._call("sendMessage", body)

    def send_chat_action(self, chat_id: int, action: str = "typing") -> dict:
        return self._call("sendChatAction", {"chat_id": chat_id, "action": action})

    def answer_callback(self, callback_id: str, text: str = "") -> dict:
        return self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def edit_reply_markup(self, chat_id: int, message_id: int) -> dict:
        """Clears the buttons once a choice has been consumed."""
        return self._call("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": []},
        })

    # ---------------------------------------------------------------- files

    def send_document(self, chat_id: int, path: str, caption: str = "") -> dict:
        return self._upload("sendDocument", "document", path, _fields(chat_id, caption))

    def send_photo(self, chat_id: int, path: str, caption: str = "") -> dict:
        return self._upload("sendPhoto", "photo", path, _fields(chat_id, caption))

    def get_file_bytes(self, file_id: str, max_bytes: int = DEFAULT_DOWNLOAD_BYTES) -> dict:
        """Download a file sent to the bot. Returns {"ok": True, "content": bytes}."""
        info = self._call("getFile", {"file_id": file_id})
        if not info.get("ok"):
            return info
        result = info.get("result")
        if not isinstance(result, dict):
            return _failure("unexpected getFile result")
        path = result.get("file_path")
        if not path:
            return _failure("file has no path on Telegram")
        if (result.get("file_size") or 0) > max_bytes:
            return _failure(f"file is larger than {max_bytes} bytes")

        url = f"{self._file_base}/{path}"
        resp = self._exchange("download", lambda: self._client.get(url, timeout=UPLOAD_TIMEOUT_S))
        if isinstance(resp, dict):
            return resp
        if resp.status_code != 200:
            return _failure(f"download HTTP {resp.status_code}", resp.status_code)
        if len(resp.content) > max_bytes:
            return _failure(f"download is larger than {max_bytes} bytes")
        return {"ok": True, "content": resp.content}
