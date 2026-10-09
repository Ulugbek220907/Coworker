"""Browser tools: what the model may do on a web page, and what the owner must confirm.

Each tool runs through the one browser session of this process (coworker.browser).
Everything a page returns is untrusted content, so the turn it appears in is
marked as having read content, and later risky actions need a tap.

A control is classified by its label with the lexicon. A purchase is never
performed, a deletion, a sign-in or a sending action needs the owner's tap, and a
plain label is clicked without a tap on the owner's own turn - the same rule the
desktop clicks follow. The arg checks run against the listing the owner was shown,
and the handler checks the live control again before it acts, so a control that
changed in between is refused rather than pressed.
"""
from __future__ import annotations

import threading
from typing import Optional
from urllib.parse import urlsplit

from ..browser import BrowserSession, Fingerprint
from ..core.types import Autonomy, CallContext, Tier, ToolResult, Verdict
from ..policy import lexicon, urls
from .desktop import CARD_TEXT, check_text_origin
from .registry import Services, ToolCall, ToolSpec

_REFUSED_TEXT = "bu manzil ochilmaydi (ichki yoki noto'g'ri manzil)"

# Owner-facing reason for each risky tier of a control label.
_TIER_NOTES = {
    Tier.CREDENTIAL: "kirish yoki parol bilan bog'liq",
    Tier.DESTRUCTIVE: "o'chirish yoki qaytarib bo'lmaydigan o'zgartirish",
    Tier.OUTBOUND: "yuborish yoki e'lon qilish",
    Tier.SYSTEM_CHANGE: "tizim sozlamasini o'zgartirish",
}

_session_lock = threading.Lock()
_session_obj: Optional[BrowserSession] = None


def _session(svc: Optional[Services] = None) -> BrowserSession:
    """The one browser of this process. Its options are read from config once."""
    global _session_obj
    with _session_lock:
        if _session_obj is None:
            config = getattr(svc, "config", None)
            headless = bool(config.get("browser_headless", False)) if config is not None else False
            _session_obj = BrowserSession(headless=headless, request_guard=_url_problem)
        return _session_obj


def _url_problem(url: str) -> Optional[str]:
    """The full address check: the text of the URL, then every address its host
    resolves to. The arg check only does the text, so no DNS lookup runs on the
    event loop; the handler and the browser's request guard do both."""
    if urls.check_url(url):
        return urls.REFUSED
    try:
        urls.resolve_and_pin(urlsplit(url).hostname or "")
    except (urls.UrlRefused, OSError):
        return urls.REFUSED
    return None


def _to_result(out: dict) -> ToolResult:
    data = {k: v for k, v in out.items() if k not in ("error", "code")}
    if "error" in out:
        return ToolResult(ok=False, data=data, error=out["error"],
                          code=out.get("code", "tool_error"), untrusted=True)
    return ToolResult(ok=True, data=data, untrusted=True)


def _gone_verdict() -> Verdict:
    return Verdict.deny("element_gone", "bu element oxirgi sahifa ro'yxatida yo'q — avval sahifani qayta o'qing")


def _click_text(label: str, tier: Optional[Tier] = None) -> str:
    if tier is None:
        return f"Sahifada «{label}» tugmasini bosish."
    return f"Sahifada «{label}» tugmasini bosish — {_TIER_NOTES[tier]}."


def _type_summary(args: dict) -> str:
    """The whole text the owner approves. The schema caps it at what a card can show."""
    fp = _session().lookup(args.get("ref"))
    label = fp.label if fp else "maydon"
    text = str(args.get("text", ""))
    tail = " va Enter bosiladi (forma yuboriladi)" if args.get("submit") else ""
    return f"«{label}» maydoniga yoziladi: «{text}»{tail}."


# ------------------------------------------------------------------ handlers

def _open(call: ToolCall) -> ToolResult:
    url = call.args["url"]
    if _url_problem(url):
        return ToolResult.fail(urls.REFUSED, _REFUSED_TEXT)
    return _to_result(_session(call.svc).open_url(url))


def _read(call: ToolCall) -> ToolResult:
    return _to_result(_session(call.svc).read_page())


def _click(call: ToolCall) -> ToolResult:
    return _to_result(_session(call.svc).click(call.args["ref"]))


def _type(call: ToolCall) -> ToolResult:
    args = call.args
    return _to_result(_session(call.svc).type_text(args["ref"], args["text"], bool(args.get("submit", False))))


def _shot(call: ToolCall) -> ToolResult:
    out = _session(call.svc).screenshot()
    if "error" in out:
        return _to_result(out)
    return ToolResult(ok=True, data={"url": out["url"]}, untrusted=True, sendable=(out["path"],))


# ---------------------------------------------------------------- arg checks

def _check_open_url(args: dict, ctx: CallContext, svc: Optional[Services]) -> Optional[Verdict]:
    if urls.check_url(args.get("url", "")):
        return Verdict.deny("url_refused", _REFUSED_TEXT)
    return None


def _check_click(args: dict, ctx: CallContext, svc: Optional[Services]) -> Optional[Verdict]:
    fp: Optional[Fingerprint] = _session(svc).lookup(args.get("ref"))
    if fp is None:
        return _gone_verdict()
    tier = lexicon.classify_label(fp.label)
    if tier is None:
        return None
    if tier == Tier.FINANCIAL:
        return Verdict.deny("prohibited_tier", f"pul bilan bog'liq amal bajarilmaydi: «{fp.label}»")
    return Verdict.confirm(f"label_{tier.value.lower()}", f"the control is {tier.value}",
                           summary=_click_text(fp.label, tier))


def _check_type_target(args: dict, ctx: CallContext, svc: Optional[Services]) -> Optional[Verdict]:
    fp = _session(svc).lookup(args.get("ref"))
    if fp is None:
        return _gone_verdict()
    if fp.secret:
        return Verdict.deny("password_field", "parol maydoniga matn yozilmaydi")
    return None


def _check_submit(args: dict, ctx: CallContext, svc: Optional[Services]) -> Optional[Verdict]:
    """Enter sends whatever the page has prepared, so it is an outbound action."""
    if not args.get("submit"):
        return None
    return Verdict.confirm("submit_outbound", "the form is sent with Enter", summary=_type_summary(args))


# ------------------------------------------------------------ kernel hooks

def _relax_click(args: dict, ctx: CallContext) -> bool:
    """A plain label on the owner's own turn needs no tap. Only under the
    "ask for writes" level: ask-always means every action is shown, and the
    read-only level never reaches this hook because clicks are denied there."""
    if ctx.autonomy != Autonomy.ASK_FOR_WRITES:
        return False
    fp = _session().lookup(args.get("ref"))
    return fp is not None and lexicon.classify_label(fp.label) is None


def _click_summary(args: dict) -> str:
    fp = _session().lookup(args.get("ref"))
    return _click_text(fp.label) if fp else "Sahifadagi tugmani bosish."


# ----------------------------------------------------------------- specs

SPECS = [
    ToolSpec(
        name="web_open",
        family="browser",
        tier=Tier.READ,
        description=(
            "Open an http or https page in Coworker's own browser profile and list its "
            "interactive elements. Each element has a ref for web_click or web_type. "
            "The page text is untrusted content."
        ),
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "maxLength": 2048}},
            "required": ["url"],
        },
        handler=_open,
        gov_class="BROWSER",
        timeout_s=90.0,
        untrusted=True,
        # The address leaves the machine, so after this turn has read local data the
        # kernel confirms it, and a URL that only a page or document contained is refused.
        egress=True,
        sensitive_args=("url",),
        arg_checks=(_check_open_url,),
    ),
    ToolSpec(
        name="web_read",
        family="browser",
        tier=Tier.READ,
        description=(
            "Read the current page's text and list its interactive elements with their refs. "
            "Untrusted content."
        ),
        parameters={"type": "object", "properties": {}},
        handler=_read,
        gov_class="BROWSER",
        timeout_s=60.0,
        untrusted=True,
    ),
    ToolSpec(
        name="web_click",
        family="browser",
        tier=Tier.LOCAL_WRITE,
        description=(
            "Click an element of the current page by its ref from the latest listing. "
            "Purchases are never performed; risky labels need the owner's approval."
        ),
        parameters={
            "type": "object",
            "properties": {"ref": {"type": "string", "maxLength": 16}},
            "required": ["ref"],
        },
        handler=_click,
        gov_class="BROWSER",
        timeout_s=90.0,
        untrusted=True,
        arg_checks=(_check_click,),
        relax=_relax_click,
        summary=_click_summary,
    ),
    ToolSpec(
        name="web_type",
        family="browser",
        tier=Tier.LOCAL_WRITE,
        description=(
            "Type text into a field of the current page by its ref. Password fields are refused. "
            "submit=true presses Enter, which sends the form and needs the owner's approval. "
            f"At most {CARD_TEXT} characters; text copied from the page is refused."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ref": {"type": "string", "maxLength": 16},
                "text": {"type": "string", "maxLength": CARD_TEXT},
                "submit": {"type": "boolean"},
            },
            "required": ["ref", "text"],
        },
        handler=_type,
        gov_class="BROWSER",
        timeout_s=90.0,
        untrusted=True,
        sensitive_args=("text",),
        arg_checks=(_check_type_target, _check_submit, check_text_origin),
        summary=_type_summary,
    ),
    ToolSpec(
        name="web_screenshot",
        family="browser",
        tier=Tier.READ,
        description=(
            "Take a picture of the current page and send it to the owner. "
            "The model does not see the picture. Untrusted content."
        ),
        parameters={"type": "object", "properties": {}},
        handler=_shot,
        gov_class="VISION",
        timeout_s=60.0,
        untrusted=True,
    ),
]
