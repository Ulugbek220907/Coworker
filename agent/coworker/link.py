"""Outbound WebSocket link from the laptop to the relay.

The agent always dials out, so the machine needs no port forwarding, no static
IP and no firewall change - it works behind any home router. Two keepalives
run alongside the socket: a protocol ping, and a periodic GET /healthz, which
is what stops Render's free tier from spinning the relay down while the
laptop sits idle.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable

import httpx
import websockets

from .config import Config

log = logging.getLogger("link")

PING_EVERY = 30.0
HEALTH_EVERY = 240.0        # comfortably under Render's 15-minute idle window
BACKOFF_MAX = 60.0

Handler = Callable[[dict], Awaitable[None]]
StatusCb = Callable[[str, str], None]  # (state, detail)


class Link:
    def __init__(self, cfg: Config, on_frame: Handler, on_status: StatusCb | None = None) -> None:
        self.cfg = cfg
        self.on_frame = on_frame
        self.on_status = on_status or (lambda *_: None)
        self._ws: Any = None
        self._stop = asyncio.Event()
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=20.0))
        self.connected = False

    # ------------------------------------------------------------------ run

    async def run(self) -> None:
        """Connect, and keep reconnecting until :meth:`stop` is called."""
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self.on_status("connecting", self.cfg.get("server_url", ""))
                async with websockets.connect(
                    self.cfg.ws_url,
                    ping_interval=PING_EVERY,
                    ping_timeout=PING_EVERY * 2,
                    max_size=8 * 1024 * 1024,
                    open_timeout=25,
                ) as ws:
                    self._ws = ws
                    await self._hello(ws)
                    self.connected = True
                    backoff = 2.0
                    self.on_status("online", self.cfg.pair_code)
                    await self._pump(ws)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.on_status("offline", _short(exc))
                log.info("link down: %s", _short(exc))
            finally:
                self.connected = False
                self._ws = None

            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.7, BACKOFF_MAX)

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        await self._http.aclose()

    # -------------------------------------------------------------- internals

    async def _hello(self, ws: Any) -> None:
        await ws.send(json.dumps({
            "type": "hello",
            "agent_id": self.cfg.get("agent_id"),
            "name": self.cfg.get("name"),
            "pair_code": self.cfg.pair_code,
            "chats": self.cfg.chats,
            "relay_token": self.cfg.get("relay_token", ""),
        }))

    async def _pump(self, ws: Any) -> None:
        """Read frames until the socket dies, with the health ping alongside."""
        health = asyncio.create_task(self._health_loop())
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if frame.get("type") in ("pong", "hello_ok"):
                    continue
                # Handle out of band so a slow search never blocks the socket.
                asyncio.create_task(self._safe_handle(frame))
        finally:
            health.cancel()

    async def _safe_handle(self, frame: dict) -> None:
        try:
            await self.on_frame(frame)
        except Exception:
            log.exception("frame handler failed")

    async def _health_loop(self) -> None:
        base = str(self.cfg.get("server_url", "")).rstrip("/")
        while True:
            await asyncio.sleep(HEALTH_EVERY)
            try:
                await self._http.get(f"{base}/healthz", timeout=20)
            except Exception:
                pass  # the reconnect loop is the real safety net

    # -------------------------------------------------------------- outbound

    async def send(self, frame: dict) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(frame, ensure_ascii=False))
            return True
        except Exception as exc:
            log.info("send failed: %s", _short(exc))
            return False

    async def reply(self, chat_id: int, text: str, buttons: list | None = None) -> bool:
        frame: dict[str, Any] = {"type": "reply", "chat_id": chat_id, "text": text}
        if buttons:
            frame["buttons"] = buttons
        return await self.send(frame)

    async def typing(self, chat_id: int) -> None:
        await self.send({"type": "typing", "chat_id": chat_id})

    async def push_state(self) -> bool:
        """Tell the relay which chats are trusted - it keeps no database."""
        return await self.send({
            "type": "state",
            "chats": self.cfg.chats,
            "pair_code": self.cfg.pair_code,
        })

    async def upload(self, chat_id: int, path: str, caption: str = "") -> bool:
        """Stream a document to the relay, which forwards it to Telegram."""
        base = str(self.cfg.get("server_url", "")).rstrip("/")
        try:
            with open(path, "rb") as fh:
                files = {"file": (os.path.basename(path), fh, "application/octet-stream")}
                data = {
                    "agent_id": str(self.cfg.get("agent_id")),
                    "chat_id": str(chat_id),
                    "caption": caption,
                    "relay_token": str(self.cfg.get("relay_token", "")),
                }
                r = await self._http.post(f"{base}/upload", data=data, files=files)
            if r.status_code != 200:
                log.warning("upload rejected: %s %s", r.status_code, r.text[:200])
                return False
            return bool(r.json().get("ok"))
        except Exception as exc:
            log.warning("upload failed: %s", _short(exc))
            return False


def _short(exc: Exception | str) -> str:
    return str(exc).strip().splitlines()[0][:120] if str(exc).strip() else exc.__class__.__name__
