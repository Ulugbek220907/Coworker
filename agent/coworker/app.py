"""Wires the link, the brain, memory and speech-to-text together.

Everything the relay pushes arrives here as a frame. Work for a given chat is
serialised behind a per-chat lock so two quick messages cannot interleave two
disk searches and confuse each other.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Callable

from .brain import Brain
from .config import Config
from .link import Link
from .llm import LLM
from .memory import MemoryStore
from .stt import Transcriber

log = logging.getLogger("app")

# A wrong pairing code should not be brute-forceable from Telegram.
MAX_PAIR_ATTEMPTS = 5
PAIR_LOCKOUT = 600.0


class CoworkerAgent:
    def __init__(self, cfg: Config, status: Callable[[str, str], None] | None = None) -> None:
        self.cfg = cfg
        self.status = status or (lambda *_: None)
        self.memory = MemoryStore()
        self.llm = LLM(
            base_url=cfg.get("llm_base_url"),
            api_key=cfg.get("llm_api_key"),
            model=cfg.get("llm_model"),
        )
        self.stt = Transcriber(
            engine=cfg.get("stt_engine", "auto"),
            model=cfg.get("stt_model", "base"),
            language="uz" if cfg.get("reply_language") == "uz" else "ru",
        )
        self.link = Link(cfg, self._on_frame, self._on_link_status)
        self.brain = Brain(cfg, self.llm, self._send_file, self._note)

        self._locks: dict[int, asyncio.Lock] = {}
        self._pending_options: dict[int, list[str]] = {}
        self._current_chat: int | None = None
        self._bad_pairs: list[float] = []

    # ------------------------------------------------------------------ run

    async def run(self) -> None:
        await self.link.run()

    async def stop(self) -> None:
        await self.link.stop()
        await self.llm.close()

    # --------------------------------------------------------------- frames

    async def _on_frame(self, frame: dict) -> None:
        kind = frame.get("type")
        if kind == "connect":
            await self._on_connect(frame)
        elif kind == "message":
            await self._on_message(frame)
        elif kind == "callback":
            await self._on_callback(frame)

    async def _on_connect(self, frame: dict) -> None:
        chat_id = int(frame["chat_id"])
        code = str(frame.get("code", "")).strip()
        who = str(frame.get("user", "?"))

        now = time.monotonic()
        self._bad_pairs = [t for t in self._bad_pairs if now - t < PAIR_LOCKOUT]
        if len(self._bad_pairs) >= MAX_PAIR_ATTEMPTS:
            await self.link.reply(chat_id, "⛔ Juda ko'p urinish. 10 daqiqadan keyin qayta urining.")
            return

        if code != self.cfg.pair_code:
            self._bad_pairs.append(now)
            self._note(f"Noto'g'ri kod: {who}")
            await self.link.reply(chat_id, "❌ Kod noto'g'ri.")
            return

        fresh = self.cfg.authorize(chat_id)
        await self.link.push_state()
        self._note(f"Ulandi: {who}" if fresh else f"Qayta ulandi: {who}")
        self.status("paired", who)
        await self.link.reply(
            chat_id,
            f"✅ «{self.cfg.get('name')}» kompyuteriga ulandingiz.\n\n"
            "Endi shunchaki yozing: «zavod bilan shartnoma kerak edi» "
            "yoki ovozli xabar yuboring.",
        )

    async def _on_message(self, frame: dict) -> None:
        chat_id = int(frame["chat_id"])
        if chat_id not in self.cfg.chats:
            await self.link.reply(chat_id, "❌ Bu chat ruxsat etilmagan.")
            return

        if frame.get("kind") == "voice":
            text = await self._transcribe(chat_id, frame)
            if not text:
                return
            await self.link.reply(chat_id, f"🎤 «{text}»")
        else:
            text = str(frame.get("text", "")).strip()

        if not text:
            return

        low = text.lower()
        if low.startswith("/forget"):
            self.memory.get(chat_id).clear()
            await self.link.reply(chat_id, "🧹 Suhbat tozalandi. Eslab qolingan papkalar saqlanib qoldi.")
            return
        if low.startswith("/reset"):
            self.memory.get(chat_id).forget_all()
            await self.link.reply(chat_id, "🧹 Hammasi tozalandi.")
            return
        if low.startswith("/disconnect"):
            self.cfg.revoke(chat_id)
            await self.link.push_state()
            await self.link.reply(chat_id, "🔌 Uzildi.")
            return

        await self._think(chat_id, text)

    async def _on_callback(self, frame: dict) -> None:
        chat_id = int(frame["chat_id"])
        if chat_id not in self.cfg.chats:
            return
        data = str(frame.get("data", ""))
        options = self._pending_options.get(chat_id, [])

        choice = data
        if data.startswith("opt:"):
            try:
                choice = options[int(data.split(":", 1)[1])]
            except (ValueError, IndexError):
                choice = ""
        if not choice:
            await self.link.reply(chat_id, "Qaytadan so'rang.")
            return

        self._pending_options.pop(chat_id, None)
        await self._think(chat_id, choice)

    # ---------------------------------------------------------------- worker

    async def _think(self, chat_id: int, text: str) -> None:
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        if lock.locked():
            await self.link.reply(chat_id, "⏳ Avvalgi so'rov bajarilyapti...")
            return

        async with lock:
            self._current_chat = chat_id
            self.status("working", text[:60])
            typing = asyncio.create_task(self._keep_typing(chat_id))
            try:
                mem = self.memory.get(chat_id)
                payload = await self.brain.handle(mem, text)
            except Exception as exc:
                log.exception("think failed")
                payload = {"text": f"⚠️ Xato: {str(exc)[:150]}"}
            finally:
                typing.cancel()
                self._current_chat = None

            if payload.get("options"):
                self._pending_options[chat_id] = payload["options"]
            await self.link.reply(chat_id, payload.get("text", ""), payload.get("buttons"))
            self.status("online", self.cfg.pair_code)

    async def _keep_typing(self, chat_id: int) -> None:
        """Telegram clears the indicator after ~5s; a disk sweep outlives that."""
        try:
            while True:
                await self.link.typing(chat_id)
                await asyncio.sleep(4.5)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ stt

    async def _transcribe(self, chat_id: int, frame: dict) -> str:
        if not self.cfg.get("stt_enabled", True):
            await self.link.reply(chat_id, "🎤 Ovozli xabar o'chirilgan. Matn yozing.")
            return ""

        try:
            audio = base64.b64decode(frame.get("audio_b64", ""))
        except Exception:
            return ""

        self.status("working", "ovoz o'girilmoqda")
        loop = asyncio.get_running_loop()
        text, err = await loop.run_in_executor(
            None, lambda: self.stt.transcribe(audio, frame.get("mime", "audio/ogg"))
        )
        if err or not text:
            await self.link.reply(
                chat_id,
                f"🎤 Ovozni tushuna olmadim{f' ({err})' if err else ''}. Matn bilan yozing.",
            )
            return ""
        return text

    # --------------------------------------------------------------- helpers

    async def _send_file(self, path: str, caption: str) -> bool:
        chat_id = self._current_chat
        if chat_id is None:
            return False
        return await self.link.upload(chat_id, path, caption)

    def _on_link_status(self, state: str, detail: str) -> None:
        self.status(state, detail)

    def _note(self, message: str) -> None:
        log.info(message)
        self.status("note", message)
