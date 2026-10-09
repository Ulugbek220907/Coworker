"""Composition root: builds the assistant from its parts and runs it.

Nothing here decides policy. The runtime connects the Telegram poll loop, the
owner's messages and button taps, the scheduler and the desktop UI to one
orchestrator, and owns the order of start-up and shutdown.

Threads: the Telegram poll loop and the scheduler run on their own threads and
call into the asyncio loop with run_coroutine_threadsafe. Blocking Telegram and
keyring calls run in the default executor, so the loop stays responsive.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Optional

from .config import Config, config_dir
from .core.ports import OsPort
from .core.types import Provenance
from .fileindex import FileIndex
from .governor import Budget, Governor, RealOs
from .llm_base import ProviderError, build_provider
from .orchestrator.prompt import CHOICE_NOTE, FORWARD_NOTE
from .orchestrator.turn import Orchestrator, Reply
from .orchestrator.turn import TIME_SCHEDULED
from .policy.kernel import PolicyKernel
from .safety import ApprovalBroker, KillSwitch, Pairing
from .scheduler import Scheduler
from .store import Store
from .store.secrets import get_secret
from .stt import Transcriber
from .tools.dispatch import Dispatcher
from .tools.registry import Services, build_registry
from .transport import commands
from .transport.bot import BotApi
from .transport.gate import OwnerGate
from .transport.outbox import Outbox
from .transport.poll import PollLoop

log = logging.getLogger("runtime")

TOKEN_SECRET = "telegram_bot_token"
MAINTENANCE_S = 60.0
# Telegram marks a message forwarded through any of these fields. Forwarded text is
# someone else's words, even when the owner sent it.
FORWARD_FIELDS = ("forward_origin", "forward_from", "forward_from_chat", "forward_sender_name",
                  "forward_date", "forward_signature")
LOCAL_WAIT_TEXT = ("🖥 Telegramda «Ha» bosildi. Bu amal kompyuterda ham tasdiqlanishi kerak; "
                   "u tasdiqlangach, «Ha» ni yana bosing.")

HELP_TEXT = (
    "🤖 Men kompyuteringizdaman. Oddiy tilda yozing yoki ovozli xabar yuboring.\n\n"
    "Misollar:\n"
    "  · «zavod bilan shartnoma kerak edi»\n"
    "  · «hisobotda foyda qancha?»\n"
    "  · «Telegram ni och»\n"
    "  · «har kuni 09:00 da hisobotni tekshir»\n\n"
    "Buyruqlar:\n"
    "/status — holat\n"
    "/stop — hozirgi ishlarni to'xtatish\n"
    "/panic — hamma amallarni to'xtatish (qayta yoqish kompyuterdan)\n"
    "/forget — suhbatni tozalash\n"
    "/facts — eslab qolingan ma'lumotlar\n"
    "/jobs — rejalashtirilgan ishlar\n"
    "/reminders — eslatmalar\n"
    "/audit — so'nggi amallar"
)


class Runtime:
    """Everything the agent owns, built once and started by ``run``."""

    def __init__(
        self,
        cfg: Config,
        *,
        status: Optional[Callable[[str, str], None]] = None,
        os_port: Optional[OsPort] = None,
        api: Optional[BotApi] = None,
        provider: Any = None,
        store_path: Any = None,
    ) -> None:
        self.cfg = cfg
        self._status = status or (lambda state, detail: None)
        home = config_dir()

        self.store = Store(store_path or home / "coworker.db")
        self.kill = KillSwitch(self.store)
        self.pairing = Pairing(self.store)
        self.gate = OwnerGate(self.store, self.pairing)
        self.approvals = ApprovalBroker(self.store)
        self.os: OsPort = os_port or RealOs()
        self.governor = Governor(self.os)
        self.budget = Budget(self.store)
        self.index = FileIndex(home / "fileindex.db", governor=self.governor)
        self.stt = Transcriber(
            engine=str(cfg.get("stt_engine", "auto")),
            model=str(cfg.get("stt_model", "base")),
            language="uz" if cfg.get("reply_language") == "uz" else "ru",
        )

        token = get_secret(TOKEN_SECRET) or ""
        self.api: Optional[BotApi] = api or (BotApi(token) if token else None)
        self.outbox: Optional[Outbox] = Outbox(self.api, self.store) if self.api else None

        self.llm = provider
        if self.llm is None:
            try:
                self.llm = build_provider(cfg, get_secret)
            except ProviderError as exc:
                log.warning("AI is not configured: %s", exc)

        self.registry = build_registry()
        self.kernel = PolicyKernel()
        self.services = Services(
            store=self.store,
            governor=self.governor,
            budget=self.budget,
            index=self.index,
            outbox=self.outbox,
            approvals=self.approvals,
            llm=self.llm,
            kill=self.kill,
            os=self.os,
            config=cfg,
        )
        self.services.scheduler = Scheduler(
            self.store, run_job=self._run_job, deliver=self._deliver, kill=self.kill,
        )
        self.dispatcher = Dispatcher(self.registry, self.kernel, self.services,
                                     provider=getattr(self.llm, "name", ""))
        self.orchestrator = Orchestrator(
            registry=self.registry, dispatcher=self.dispatcher, svc=self.services,
            config=cfg, name=str(cfg.get("name") or "Coworker"),
        )
        self.poll: Optional[PollLoop] = (
            PollLoop(self.api, self.store, self.gate, self._on_update, on_stopped=self._poll_stopped)
            if self.api else None
        )

        self._stop = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ----------------------------------------------------------------- running

    async def run(self) -> None:
        """Start the poll loop and the scheduler, then keep maintenance going until stopped."""
        self._loop = asyncio.get_running_loop()
        # Sync tool handlers that must await the model client reach the loop through here.
        self.services.loop = self._loop
        orphans = self.store.audit_orphans()
        if orphans:
            log.warning("%d earlier action(s) have no recorded outcome and may have run", len(orphans))
        self.services.scheduler.start()
        self.index.start(self._stop)
        if self.poll is not None:
            threading.Thread(target=self.poll.run, args=(self._stop,),
                             name="telegram-poll", daemon=True).start()
            self._status("online", self.pairing_detail())
        else:
            self._status("offline", "Telegram tokeni kiritilmagan")
        while not self._stop.is_set():
            await asyncio.sleep(MAINTENANCE_S)
            self.store.approvals_expire()

    def stop(self) -> None:
        """Stop everything: cancel running work, end the poll loop, stop the scheduler."""
        self._stop.set()
        self.kill.stop()
        self.services.scheduler.stop()
        self.index.stop()
        if self.api is not None:
            self.api.close()
        self._status("offline", "to'xtatildi")

    def _poll_stopped(self, reason: str) -> None:
        self._status("offline", reason)

    def pairing_detail(self) -> str:
        return "ulash kodi: " + self.pairing.issue_code()

    # --------------------------------------------------------- inbound updates

    def _on_update(self, update: dict) -> None:
        """Called on the poll thread with an update the gate already admitted."""
        if self._loop is None:
            return
        if "callback_query" in update:
            coro = self._on_callback(update["callback_query"])
        elif "message" in update:
            coro = self._on_message(update["message"])
        else:
            return
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(_log_failure)

    async def _on_message(self, msg: dict) -> None:
        chat_id = int(msg["chat"]["id"])
        text = str(msg.get("text") or msg.get("caption") or "").strip()
        voice = msg.get("voice") or msg.get("audio")
        forwarded = is_forwarded(msg)

        if not text and voice:
            text = await self._transcribe(voice)
            if not text:
                await self._say(chat_id, "🎤 Ovozni tushuna olmadim. Matn bilan yozing.")
                return
            await self._say(chat_id, f"🎤 «{text}»")
        if not text:
            await self._say(chat_id, "Matn yoki ovozli xabar yuboring.")
            return

        # A forwarded message is never a command: its text is a third party's, not the owner's.
        name, args = (None, "") if forwarded else commands.parse(text)
        if name:
            reply = await self._command(name, args, chat_id)
        elif self.llm is None:
            reply = Reply("⚠️ AI sozlanmagan. Sozlamalarda model va kalitni kiriting.")
        elif forwarded:
            reply = await self.orchestrator.handle_owner(
                chat_id, text, provenance=Provenance.CONTENT, note=FORWARD_NOTE,
            )
        else:
            # An answer in text settles the open questions, so their buttons go.
            if self.outbox is not None:
                await asyncio.to_thread(self.outbox.retire_asks, chat_id)
            reply = await self.orchestrator.handle_owner(chat_id, text)
        await self._reply(chat_id, reply)

    async def _on_callback(self, cq: dict) -> None:
        message = cq.get("message") or {}
        chat_id = int(message.get("chat", {}).get("id", 0))
        actor = int(cq.get("from", {}).get("id", 0))
        data = str(cq.get("data", ""))
        await asyncio.to_thread(self._answer, cq.get("id", ""))

        owner = self.pairing.owner()
        if owner is None or actor != owner[0] or chat_id != owner[1]:
            return  # the gate admits only the owner; this is the second check

        if data.startswith("opt:"):
            reply = await self._choose_option(chat_id, data, message)
        else:
            reply = await self._decide(chat_id, data, actor, owner[0], message)
        if reply is not None:
            await self._reply(chat_id, reply)

    async def _choose_option(self, chat_id: int, data: str, message: dict) -> Optional[Reply]:
        try:
            index = int(data.split(":", 1)[1])
        except ValueError:
            return None
        # The question is the one whose message carried the tapped button. Its options
        # are claimed here, so a second tap on it, or a tap on a retired question, finds nothing.
        choice = self.outbox.claim_ask(chat_id, message.get("message_id"), index) if self.outbox else None
        if choice is None:
            return Reply("⚠️ Bu tugma eskirgan. Savolni qaytadan so'rang.")
        option, origin = choice
        await self._strip_buttons(chat_id, message)
        if self.llm is None:
            return Reply("⚠️ AI sozlanmagan.")
        provenance = Provenance(origin)
        note = CHOICE_NOTE if provenance != Provenance.OWNER else ""
        return await self.orchestrator.handle_owner(chat_id, option, provenance=provenance, note=note)

    async def _decide(self, chat_id: int, data: str, actor: int, owner_id: int,
                      message: dict) -> Optional[Reply]:
        parsed = ApprovalBroker.parse_callback(data)
        if parsed is None:
            return None
        approval_id, nonce, approve = parsed
        if approve and self.orchestrator.awaiting_local(approval_id, nonce):
            # The laptop half is still missing. The buttons stay, so the owner can tap again.
            return Reply(LOCAL_WAIT_TEXT)
        await self._strip_buttons(chat_id, message)
        if approve:
            return await self.orchestrator.run_approved(approval_id, nonce, actor, owner_id)
        return self.orchestrator.decline(approval_id, nonce, actor, owner_id)

    # ---------------------------------------------------------------- commands

    async def _command(self, name: str, args: str, chat_id: int) -> Reply:
        store = self.store
        if name in ("start", "help"):
            return Reply(HELP_TEXT)
        if name == "status":
            return Reply(self.status_text())
        if name == "stop":
            self.kill.stop()
            return Reply("⏹ To'xtatildi. Hozirgi ishlar bekor qilindi.")
        if name == "panic":
            self.kill.panic()
            return Reply("🛑 PANIC: hamma amallar to'xtatildi. Qayta yoqish faqat kompyuterdan mumkin.")
        if name == "forget":
            store.turns_clear(chat_id)
            return Reply("🧹 Suhbat tozalandi. Eslab qolingan ma'lumotlar saqlandi.")
        if name == "reset":
            store.turns_clear(chat_id)
            store.summary_set(chat_id, "")
            return Reply("🧹 Suhbat tozalandi.")
        if name == "facts":
            rows = store.facts_list(chat_id, limit=30)
            return Reply("\n".join(f"· {r['text']}" for r in rows) or "Hozircha ma'lumot yo'q.")
        if name == "jobs":
            rows = store.jobs_list(chat_id)
            return Reply("\n".join(f"· {r['name']} ({_job_state(r)})" for r in rows)
                         or "Rejalashtirilgan ish yo'q.")
        if name == "reminders":
            rows = store.reminders_list(chat_id)
            return Reply("\n".join(f"· {r['text']}" for r in rows) or "Eslatma yo'q.")
        if name == "audit":
            rows = store.audit_tail(limit=10)
            lines = [f"{r.get('tool', '?')} {r.get('decision', '')} {r.get('code', '')}".strip() for r in rows]
            unknown = len(store.audit_orphans())
            if unknown:
                lines.insert(0, f"⚠️ {unknown} ta amal natijasi noma'lum; bajarilgan bo'lishi mumkin.")
            return Reply("\n".join(lines) or "Hozircha amallar yo'q.")
        return Reply("")

    def status_text(self) -> str:
        usage = self.budget.usage()
        pressure = self.governor.pressure()
        mode = "PANIC" if self.kill.is_panic() else str(self.cfg.get("autonomy", "ask_for_writes"))
        lines = [
            f"✅ «{self.cfg.get('name')}» ishlayapti.",
            f"Rejim: {mode}",
            f"Yuk: CPU {pressure.get('cpu_pct', '?')}%, RAM {pressure.get('free_ram_mb', '?')} MB",
            f"Bugungi sarf: {usage['units']['used']} / {usage['units']['ceiling']} birlik",
        ]
        if self.llm is None:
            lines.append("⚠️ AI sozlanmagan")
        return "\n".join(lines)

    # ------------------------------------------------------------ outbound

    async def _reply(self, chat_id: int, reply: Reply) -> None:
        if not reply.text and not reply.buttons:
            return
        await self._send(chat_id, reply.text, reply.buttons or None)

    async def _say(self, chat_id: int, text: str) -> None:
        await self._send(chat_id, text, None)

    async def _send(self, chat_id: int, text: str, buttons: Any) -> None:
        if self.outbox is None:
            log.warning("no Telegram connection; reply dropped")
            return
        await asyncio.to_thread(self.outbox.text, chat_id, text, buttons)

    def _deliver(self, chat_id: int, text: str) -> bool:
        """Scheduler callback, on the scheduler thread. True only when Telegram accepted the text."""
        if self.outbox is None:
            return False
        return bool(self.outbox.text(chat_id, text).get("ok"))

    def _run_job(self, job: dict) -> str:
        """Scheduler callback, on the scheduler thread: run one job on the loop."""
        if self._loop is None or self.llm is None:
            raise RuntimeError("the assistant is not running or AI is not configured")
        future = asyncio.run_coroutine_threadsafe(
            self.orchestrator.run_scheduled(str(job["instruction"]), int(job["chat_id"])), self._loop,
        )
        return future.result(timeout=TIME_SCHEDULED + 30)

    async def _transcribe(self, voice: dict) -> str:
        if not self.cfg.get("stt_enabled", True) or self.api is None:
            return ""
        got = await asyncio.to_thread(self.api.get_file_bytes, str(voice.get("file_id", "")))
        if not got.get("ok"):
            return ""
        audio = got.get("content") or b""
        mime = str(voice.get("mime_type") or "audio/ogg")
        text, err = await self.governor.run(
            "STT", lambda: self.stt.transcribe(audio, mime), timeout_s=120.0,
        )
        return "" if err else str(text or "")

    async def _strip_buttons(self, chat_id: int, message: dict) -> None:
        msg_id = message.get("message_id")
        if self.api is not None and msg_id:
            await asyncio.to_thread(self.api.edit_reply_markup, chat_id, int(msg_id))

    def _answer(self, callback_id: str) -> None:
        if self.api is not None and callback_id:
            self.api.answer_callback(callback_id)

    # --------------------------------------------------------------- for the UI

    def set_autonomy(self, level: str) -> None:
        self.cfg.set("autonomy", level)

    def resume_locally(self) -> bool:
        """Clear panic. Only the desktop UI may call this; Telegram has no path to it."""
        return self.kill.resume_local()


def _job_state(job: dict) -> str:
    return "to'xtatilgan" if job.get("paused") else "faol"


def is_forwarded(msg: dict) -> bool:
    return any(field in msg for field in FORWARD_FIELDS)


def _log_failure(future: Any) -> None:
    exc = future.exception()
    if exc is not None:
        log.error("update handler failed: %s", type(exc).__name__, exc_info=exc)
