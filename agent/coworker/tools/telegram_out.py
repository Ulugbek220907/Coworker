"""Telegram tools: the model's only way to send anything to the owner.

All three are OUTBOUND and self_target. The kernel therefore allows them under
the default autonomy, because they can reach only the owner's own chat. The
outbox checks the chat again, so a wrong chat id in a context still cannot
send anywhere else.

send_file is the only tool here that reads from disk. Its path must come from a
search in this conversation (requires_surfaced), and the origin check refuses a
path that appeared only in a document the owner never wrote (sensitive_args).
"""
from __future__ import annotations

import logging
import os

from ..core.types import Decision, Tier, ToolResult
from ..transport.outbox import MAX_DOCUMENT_BYTES, MAX_OPTIONS
from .registry import ToolCall, ToolSpec

log = logging.getLogger("tools.telegram")

# The button that backs the owner out of a choice. It goes last in the options. The prompt
# names the same text (orchestrator.prompt.CANCEL_OPTION); a test checks that they agree.
CANCEL_OPTION = "Bekor qilish"
NOT_CONNECTED = "Telegram ulanmagan."
SEND_FAILED = "Telegram orqali yuborib bo'lmadi."
BUDGET_TEXT = {
    "budget_exceeded": "Bugungi yuborish limiti tugagan.",
    "rate_limited": "Juda tez yuborilyapti. Biroz kuting.",
}


def _target(call: ToolCall) -> int | None:
    """The chat for this call. A scheduled run has no chat, so it uses the owner's."""
    if call.ctx.chat_id is not None:
        return call.ctx.chat_id
    return call.svc.outbox.owner_chat()


def _send_failed(result: dict) -> ToolResult:
    if result.get("code") == "not_configured":
        return ToolResult.fail("not_configured", NOT_CONNECTED)
    return ToolResult.fail("send_failed", SEND_FAILED)


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def _key(path: str) -> str:
    # Same normalisation as the kernel, so a delivered path matches later.
    return os.path.normcase(os.path.normpath(path))


def _send_file(call: ToolCall) -> ToolResult:
    svc = call.svc
    if svc.outbox is None or svc.budget is None or svc.store is None:
        return ToolResult.fail("not_configured", NOT_CONNECTED)
    path = str(call.args["path"])
    caption = str(call.args.get("caption") or "")
    if not os.path.isfile(path):
        return ToolResult.fail("arg_invalid", "Fayl topilmadi.")
    size = os.path.getsize(path)
    if size > MAX_DOCUMENT_BYTES:
        return ToolResult.fail("arg_invalid", f"Fayl juda katta: {_mb(size)}. Telegram 45 MB gacha yuboradi.")
    chat_id = _target(call)
    if chat_id is None:
        return ToolResult.fail("not_configured", NOT_CONNECTED)

    verdict = svc.budget.admit_bytes(size, chat_id)
    if verdict.decision != Decision.ALLOW:
        return ToolResult.fail(verdict.code, BUDGET_TEXT.get(verdict.code, "Yuborish ruxsat etilmadi."))

    result = svc.outbox.document(chat_id, path, caption)
    if not result.get("ok"):
        return _send_failed(result)
    name = os.path.basename(path)
    svc.store.delivered_add(chat_id, _key(path), name)
    log.info("sent %s (%d bytes)", name, size)
    return ToolResult(ok=True, data={"name": name, "bytes": size})


def _ask(call: ToolCall) -> ToolResult:
    svc = call.svc
    if svc.outbox is None:
        return ToolResult.fail("not_configured", NOT_CONNECTED)
    options = [str(o) for o in call.args.get("options") or []]
    if not options:
        return ToolResult.fail("arg_invalid", "Kamida bitta variant kerak.")
    # The outbox keeps only the first MAX_OPTIONS, so a longer list would silently lose the
    # cancel option at its end. Refuse it here, so the model asks again with a shorter list.
    if len(options) > MAX_OPTIONS:
        return ToolResult.fail("arg_invalid", f"Ko'pi bilan {MAX_OPTIONS} ta variant.")
    chat_id = _target(call)
    if chat_id is None:
        return ToolResult.fail("not_configured", NOT_CONNECTED)
    result = svc.outbox.ask(chat_id, str(call.args["question"]).strip(), options,
                            provenance=int(call.ctx.provenance))
    if not result.get("ok"):
        return _send_failed(result)
    # end_turn tells the orchestrator to stop here; the owner's tap comes next.
    return ToolResult(ok=True, data={"asked": True, "end_turn": True})


def _notify(call: ToolCall) -> ToolResult:
    svc = call.svc
    if svc.outbox is None:
        return ToolResult.fail("not_configured", NOT_CONNECTED)
    result = svc.outbox.notify(str(call.args["text"]))
    if not result.get("ok"):
        return _send_failed(result)
    return ToolResult(ok=True, data={"sent": True})


def _summary_send_file(args: dict) -> str:
    return f"Telegram orqali yuborish: {os.path.basename(str(args.get('path', '')))}"


def _summary_ask(args: dict) -> str:
    return f"Telegramda savol yuborish: {str(args.get('question', ''))[:200]}"


def _summary_notify(args: dict) -> str:
    return f"Telegramga xabar yuborish: {str(args.get('text', ''))[:200]}"


SPECS: list[ToolSpec] = [
    ToolSpec(
        name="send_file",
        family="telegram",
        tier=Tier.OUTBOUND,
        description=(
            "Send a file from this computer to the owner's Telegram chat. The path must be one "
            "that a search in this conversation returned. Pictures arrive as photos; files up to "
            "45 MB are accepted."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "maxLength": 1024, "description": "full path from a search result"},
                "caption": {"type": "string", "maxLength": 1000, "description": "optional short caption"},
            },
            "required": ["path"],
        },
        handler=_send_file,
        gov_class="TOOL",
        timeout_s=180.0,
        self_target=True,
        path_args=("path",),
        requires_surfaced=("path",),
        sensitive_args=("path",),
        summary=_summary_send_file,
    ),
    ToolSpec(
        name="ask",
        family="telegram",
        tier=Tier.OUTBOUND,
        description=(
            "Ask the owner a question in Telegram with up to six answer buttons, one per option, "
            "in the order given. Use it for a choice between names a lookup returned, and when the "
            f"owner may refuse, put \"{CANCEL_OPTION}\" last. The turn ends after this call; the owner's "
            "choice arrives later as a button tap."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "maxLength": 1000},
                "options": {
                    "type": "array",
                    "description": f"answer buttons in order; the last one may be \"{CANCEL_OPTION}\"",
                    "items": {"type": "string", "maxLength": 200},
                    "minItems": 1,
                    "maxItems": 6,
                },
            },
            "required": ["question", "options"],
        },
        handler=_ask,
        gov_class="TOOL",
        self_target=True,
        summary=_summary_ask,
    ),
    ToolSpec(
        name="notify",
        family="telegram",
        tier=Tier.OUTBOUND,
        description=(
            "Send a short status message to the owner's Telegram chat without waiting for a "
            "reply, for example to report progress or completion."
        ),
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "maxLength": 3000}},
            "required": ["text"],
        },
        handler=_notify,
        gov_class="TOOL",
        self_target=True,
        summary=_summary_notify,
    ),
]
