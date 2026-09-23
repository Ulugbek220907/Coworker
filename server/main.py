"""Coworker relay server.

Runs on Render's free tier. Its only jobs are:

  1. receive Telegram webhooks,
  2. route each message to the right desktop agent over a WebSocket,
  3. push the agent's replies (and files) back to Telegram.

It holds no database, no LLM keys, and never sees the contents of the user's
disk beyond what the agent explicitly chooses to send back. All the
intelligence lives in the desktop agent, which keeps this process small enough
to stay comfortably inside a 512 MB free instance.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import time

from fastapi import (
    FastAPI, File, Form, Header, HTTPException, Request, UploadFile,
    WebSocket, WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, PlainTextResponse

from relay import Agent, Registry
from telegram import Telegram

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("server")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
RELAY_TOKEN = os.getenv("RELAY_TOKEN", "").strip()

# Both of these used to fail OPEN - WEBHOOK_SECRET had a hardcoded default
# that is public in this repo, and RELAY_TOKEN was only enforced "if set".
# A relay that boots with no authentication is worse than one that refuses
# to boot, because nothing about it looks wrong from the outside.
if not WEBHOOK_SECRET or WEBHOOK_SECRET == "coworker-hook":
    raise SystemExit(
        "WEBHOOK_SECRET is missing or still the example value. Set it to a "
        "random string (on Render: generateValue) and redeploy."
    )
if not RELAY_TOKEN:
    raise SystemExit(
        "RELAY_TOKEN is not set. Without it anyone can attach an agent to "
        "this relay. Set it and put the same value in the desktop app."
    )
PUBLIC_URL = (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_URL") or "").rstrip("/")

def _telegram_secret(raw: str) -> str:
    """Telegram only accepts [A-Za-z0-9_-]{1,256} as a webhook secret_token.

    Render's `generateValue: true` produces base64, which contains '+', '/'
    and '=' - setWebhook then fails with "secret token contains illegal
    characters" and the bot sits there looking healthy but silent. Hashing
    keeps the value deterministic and secret while always being legal.
    """
    if not raw:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_-]{1,256}", raw):
        return raw
    return hashlib.sha256(raw.encode()).hexdigest()


# What we actually hand to Telegram and compare incoming headers against.
TELEGRAM_SECRET = _telegram_secret(WEBHOOK_SECRET)

MAX_VOICE_BYTES = 3 * 1024 * 1024
MAX_UPLOAD_BYTES = 45 * 1024 * 1024  # Telegram bots cap uploads at 50 MB

app = FastAPI(title="Coworker Relay", docs_url=None, redoc_url=None)
registry = Registry()
tg = Telegram(BOT_TOKEN) if BOT_TOKEN else None

# Surfaced on /healthz so a misregistered webhook is visible from outside
# instead of looking like a healthy but silent bot.
webhook_state: dict[str, object] = {"url": "", "ok": False, "error": "starting"}

HELP = (
    "\U0001f916 Coworker\n\n"
    "Men sizning kompyuteringizdagi hujjatlarni topib beraman.\n\n"
    "Shunchaki oddiy tilda yozing yoki ovozli xabar yuboring:\n"
    "  · «zavod bilan shartnoma kerak»\n"
    "  · «oxirgi hafta ochgan hisobotlarim»\n"
    "  · «договор с текстильным заводом»\n\n"
    "Buyruqlar:\n"
    "/connect KOD — kompyuterga ulanish\n"
    "/status — ulanish holati\n"
    "/forget — suhbatni tozalash"
)


# ----------------------------------------------------------------- lifecycle

@app.on_event("startup")
async def _startup() -> None:
    if not tg:
        log.error("TELEGRAM_BOT_TOKEN is not set - the bot half is disabled")
        return
    me = await tg.call("getMe")
    if me.get("ok"):
        log.info("bot ready: @%s", me["result"].get("username"))
    await tg.set_commands()
    # Registration runs in the background: Telegram validates the URL the
    # moment setWebhook is called, and on a cold Render deploy the service is
    # not publicly routable yet. One attempt at startup loses that race.
    asyncio.create_task(_webhook_keeper())


async def _webhook_keeper() -> None:
    """Register the webhook, retry until it sticks, then keep verifying it."""
    if not tg:
        return
    if not PUBLIC_URL:
        webhook_state["error"] = "RENDER_EXTERNAL_URL/PUBLIC_URL is not set"
        log.error("no public URL - cannot register a webhook")
        return

    want = f"{PUBLIC_URL}/tg"
    delay = 5.0
    while True:
        try:
            info = await tg.call("getWebhookInfo")
            current = (info.get("result") or {}).get("url", "")

            if current == want:
                webhook_state.update({"url": current, "ok": True, "error": ""})
                # Re-check occasionally; a redeploy or an outside call can
                # clear it, and a silently unregistered bot looks "online".
                await asyncio.sleep(600)
                delay = 5.0
                continue

            res = await tg.set_webhook(want, TELEGRAM_SECRET)
            if res.get("ok"):
                webhook_state.update({"url": want, "ok": True, "error": ""})
                log.info("webhook registered -> %s", want)
                await asyncio.sleep(600)
                delay = 5.0
                continue

            webhook_state.update({"ok": False, "error": str(res.get("description"))})
            log.warning("setWebhook failed (%s) - retrying in %.0fs",
                        res.get("description"), delay)
        except Exception as exc:
            webhook_state.update({"ok": False, "error": str(exc)[:200]})
            log.warning("webhook check failed: %s - retrying in %.0fs", exc, delay)

        await asyncio.sleep(delay)
        delay = min(delay * 2, 120.0)


@app.on_event("shutdown")
async def _shutdown() -> None:
    if tg:
        await tg.close()


# -------------------------------------------------------------------- health

@app.get("/", response_class=PlainTextResponse)
async def root() -> str:
    return f"Coworker relay is up. agents={registry.count}"


@app.get("/healthz")
async def healthz() -> JSONResponse:
    # The agent pings this so Render's free tier does not spin the service down.
    return JSONResponse({
        "ok": True,
        "agents": registry.snapshot(),
        "webhook": webhook_state,
        "ts": int(time.time()),
    })


@app.get("/setup")
async def setup(key: str = "") -> JSONResponse:
    """Force webhook registration now. Guarded by the webhook secret."""
    if not tg:
        raise HTTPException(503, "bot disabled")
    if not WEBHOOK_SECRET or key != WEBHOOK_SECRET:
        raise HTTPException(403, "bad key")
    if not PUBLIC_URL:
        raise HTTPException(500, "PUBLIC_URL is not set")
    res = await tg.set_webhook(f"{PUBLIC_URL}/tg", TELEGRAM_SECRET)
    webhook_state.update({
        "url": f"{PUBLIC_URL}/tg" if res.get("ok") else "",
        "ok": bool(res.get("ok")),
        "error": str(res.get("description") or ""),
    })
    return JSONResponse({"ok": bool(res.get("ok")), "detail": res.get("description")})


# -------------------------------------------------------------- agent socket

@app.websocket("/ws/agent")
async def ws_agent(ws: WebSocket) -> None:
    await ws.accept()
    agent: Agent | None = None
    try:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=20)
        if hello.get("type") != "hello":
            await ws.close(code=4001, reason="expected hello")
            return
        if hello.get("relay_token") != RELAY_TOKEN:
            await ws.close(code=4003, reason="bad relay token")
            return

        agent_id = str(hello.get("agent_id") or "").strip()
        if not agent_id:
            await ws.close(code=4002, reason="missing agent_id")
            return

        agent = Agent(
            agent_id=agent_id,
            ws=ws,
            name=str(hello.get("name") or "PC")[:64],
            pair_code=str(hello.get("pair_code") or "")[:16],
            chats=_chat_ids(hello.get("chats")),
        )
        await registry.add(agent)
        await ws.send_json({"type": "hello_ok", "server_time": int(time.time())})

        while True:
            frame = await ws.receive_json()
            agent.last_seen = time.time()
            await _handle_agent_frame(agent, frame)

    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception as exc:
        log.info("agent socket closed: %s", exc)
    finally:
        if agent:
            await registry.remove(agent.agent_id, ws)


async def _handle_agent_frame(agent: Agent, frame: dict) -> None:
    kind = frame.get("type")

    if kind == "ping":
        await agent.send({"type": "pong"})

    elif kind == "state":
        # The agent owns the authoritative list of chats it trusts.
        agent.chats = _chat_ids(frame.get("chats"))
        if frame.get("pair_code"):
            agent.pair_code = str(frame["pair_code"])[:16]

    elif kind == "reply" and tg:
        await tg.send_text(
            int(frame["chat_id"]),
            str(frame.get("text", "")),
            buttons=frame.get("buttons"),
            reply_to=frame.get("reply_to"),
        )

    elif kind == "typing" and tg:
        await tg.send_chat_action(int(frame["chat_id"]), frame.get("action", "typing"))


def _chat_ids(raw) -> set[int]:
    out: set[int] = set()
    for c in raw or []:
        try:
            out.add(int(c))
        except (TypeError, ValueError):
            continue
    return out


# ----------------------------------------------------------- agent -> upload

@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    agent_id: str = Form(...),
    chat_id: int = Form(...),
    caption: str = Form(""),
    relay_token: str = Form(""),
) -> JSONResponse:
    """Agents stream documents through here rather than over the WebSocket."""
    if not tg:
        raise HTTPException(503, "bot disabled")
    if relay_token != RELAY_TOKEN:
        raise HTTPException(403, "bad relay token")

    agent = registry.by_id(agent_id)
    if agent is None or chat_id not in agent.chats:
        raise HTTPException(403, "agent not authorised for this chat")

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        await tg.send_text(chat_id, "⚠️ Fayl juda katta (50 MB limit).")
        raise HTTPException(413, "file too large")

    name = file.filename or "document"
    # Images preview inline as photos; Telegram caps a photo at 10 MB, so
    # larger images still go as a document.
    is_image = name.lower().rsplit(".", 1)[-1] in ("png", "jpg", "jpeg", "webp", "gif")
    if is_image and len(content) <= 10 * 1024 * 1024:
        res = await tg.send_photo(chat_id, name, content, caption)
    else:
        res = await tg.send_document(chat_id, name, content, caption)
    return JSONResponse({"ok": bool(res.get("ok")), "error": res.get("description")})


# ---------------------------------------------------------- telegram webhook

@app.post("/tg")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> JSONResponse:
    if TELEGRAM_SECRET and x_telegram_bot_api_secret_token != TELEGRAM_SECRET:
        raise HTTPException(403, "bad secret")
    update = await request.json()
    # Answer Telegram immediately; routing happens out of band.
    asyncio.create_task(_route(update))
    return JSONResponse({"ok": True})


async def _route(update: dict) -> None:
    try:
        if "callback_query" in update:
            await _route_callback(update["callback_query"])
        elif "message" in update:
            await _route_message(update["message"])
    except Exception:
        log.exception("routing failed")


async def _route_callback(cq: dict) -> None:
    if not tg:
        return
    chat_id = cq["message"]["chat"]["id"]
    await tg.answer_callback(cq["id"])
    agent = registry.by_chat(chat_id)
    if agent is None:
        await tg.send_text(chat_id, "⚠️ Kompyuter ulanmagan.")
        return
    await tg.edit_reply_markup(chat_id, cq["message"]["message_id"])
    await agent.send({
        "type": "callback",
        "chat_id": chat_id,
        "data": cq.get("data", ""),
        "user": _who(cq.get("from", {})),
    })


async def _route_message(msg: dict) -> None:
    if not tg:
        return
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or msg.get("caption") or "").strip()
    who = _who(msg.get("from", {}))
    low = text.lower()

    if low.startswith("/start") or low.startswith("/help"):
        # Only the agent knows this chat's capabilities, so let it answer when
        # one is connected - otherwise the bot describes powers it may not have.
        agent = registry.by_chat(chat_id)
        if agent is None:
            await tg.send_text(chat_id, HELP)
            await tg.send_text(
                chat_id,
                "Boshlash uchun kompyuterda Coworker ilovasini oching va "
                "u ko'rsatgan kodni yuboring:\n\n/connect 123456",
            )
            return
        if not await agent.send({
            "type": "message", "kind": "text", "chat_id": chat_id,
            "user": who, "text": "/help",
        }):
            await tg.send_text(chat_id, HELP)
        return

    if low.startswith("/connect"):
        await _handle_connect(chat_id, text, who)
        return

    if low.startswith("/status"):
        agent = registry.by_chat(chat_id)
        if agent:
            mins = int((time.time() - agent.connected_at) // 60)
            await tg.send_text(chat_id, f"✅ «{agent.name}» ulangan ({mins} daqiqa).")
        else:
            await tg.send_text(chat_id, "❌ Kompyuter ulanmagan. /connect KOD")
        return

    agent = registry.by_chat(chat_id)
    if agent is None:
        await tg.send_text(
            chat_id,
            "❌ Kompyuter ulanmagan.\n\nKompyuterda Coworker ilovasi ochiqmi? "
            "Ochiq bo'lsa, u ko'rsatgan kod bilan: /connect 123456",
        )
        return

    voice = msg.get("voice") or msg.get("audio") or msg.get("video_note")
    if voice:
        await tg.send_chat_action(chat_id, "typing")
        raw = await tg.get_file_bytes(voice["file_id"], MAX_VOICE_BYTES)
        if raw is None:
            await tg.send_text(chat_id, "⚠️ Ovozli xabarni yuklab bo'lmadi.")
            return
        ok = await agent.send({
            "type": "message",
            "kind": "voice",
            "chat_id": chat_id,
            "user": who,
            "reply_to": msg.get("message_id"),
            "audio_b64": base64.b64encode(raw).decode(),
            "mime": voice.get("mime_type", "audio/ogg"),
        })
    elif text:
        await tg.send_chat_action(chat_id, "typing")
        ok = await agent.send({
            "type": "message",
            "kind": "text",
            "chat_id": chat_id,
            "user": who,
            "reply_to": msg.get("message_id"),
            "text": text,
        })
    else:
        await tg.send_text(chat_id, "Matn yoki ovozli xabar yuboring.")
        return

    if not ok:
        await tg.send_text(chat_id, "⚠️ Kompyuter bilan aloqa uzildi. Biroz kuting.")


async def _handle_connect(chat_id: int, text: str, who: str) -> None:
    if not tg:
        return
    parts = text.split()
    if len(parts) < 2:
        await tg.send_text(chat_id, "Kodni ham yozing:\n/connect 123456")
        return

    agent = registry.by_pair_code(parts[1].strip())
    if agent is None:
        await tg.send_text(
            chat_id,
            "❌ Kod noto'g'ri yoki kompyuter ulanmagan.\n"
            "Kompyuterdagi Coworker ilovasida ko'rsatilgan kodni tekshiring.",
        )
        return

    await agent.send({
        "type": "connect", "chat_id": chat_id, "code": parts[1].strip(), "user": who,
    })


def _who(user: dict) -> str:
    name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
    return name or user.get("username") or str(user.get("id", "?"))
