"""Helpers and panels for the desktop window.

Everything above the panel classes is pure: no Tk window is created, so the tests
check the wording, the state mapping and the redaction on their own. The panel
classes build widgets on a parent frame and are only created by ui.AgentWindow,
on the Tk thread.
"""
from __future__ import annotations

import logging
import queue
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Iterable, Mapping, Optional

from . import tray as tray_mod
from .config import DEFAULTS, Config
from .llm import PROVIDERS
from .policy.prohibited import redact as redact_secrets
from .store.secrets import SecretStoreError, get_secret, set_secret
from .tools.registry import FAMILIES
from .transport.bot import redact as redact_tokens

BG = "#12151c"
CARD = "#1a1f2a"
FG = "#e8ecf3"
MUTED = "#8a94a6"
ACCENT = "#4a9eff"
OK = "#3ddc84"
WARN = "#ffb020"
BAD = "#ff5c5c"

TOKEN_SECRET = "telegram_bot_token"   # the name runtime.TOKEN_SECRET uses
LLM_KEY_SECRET = "llm_api_key"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s | %(message)s"

AUTONOMY_LEVELS = ("ask_always", "ask_for_writes", "autonomous_readonly")
DEFAULT_AUTONOMY = "ask_for_writes"
AUTONOMY_LABELS = {
    "ask_always": "Har doim so'rash",
    "ask_for_writes": "Yozishdan oldin so'rash (standart)",
    "autonomous_readonly": "Avtonom: faqat o'qish",
}
AUTONOMY_HELP = {
    "ask_always": ("O'qishdan tashqari har bir amal, shu jumladan eslatma yozish va "
                   "o'zingizga xabar yuborish, avval sizdan tasdiq so'raydi."),
    "ask_for_writes": ("O'qish, eslatma yozish va o'zingizga xabar yuborish avtomatik. "
                       "Fayl o'zgartirish, boshqalarga xabar yuborish va tizim amallari "
                       "uchun tasdiq so'raladi."),
    "autonomous_readonly": ("Faqat o'qish, eslatma va o'zingizga xabar. Fayl o'zgartirish, "
                            "boshqalarga xabar va tizim amallari rad etiladi. Siz yo'qligingizda "
                            "ishlash uchun."),
}

FAMILY_LABELS = {
    "files": "Fayllar: qidirish, o'qish, yuborish",
    "office": "Ofis hujjatlari: jadval o'qish, PDF",
    "desktop": "Ochiq oynalarni ko'rish",
    "desktop_control": "Oynalarni boshqarish: bosish, yozish",
    "apps": "Ilovalarni ochish va boshqarish",
    "system": "Tizim: ovoz, oyna holati",
    "browser": "Brauzer: sayt ochish, to'ldirish",
    "web": "Internetdan qidirish",
    "shell": "Buyruq qatori (shell)",
    "notes": "Eslatmalar va vazifalar",
    "schedule": "Rejalashtirilgan ishlar",
    "telegram": "Telegram'ga xabar yuborish",
    "mail": "Pochta",
    "calendar": "Kalendar",
}

STATE_STYLE = {
    "starting": (WARN, "Ishga tushmoqda..."),
    "online": (OK, "Ulangan"),
    "working": (ACCENT, "Ishlayapti"),
    "offline": (BAD, "Aloqa yo'q"),
}


# --------------------------------------------------------------- pure helpers

def normalize_autonomy(value: Any) -> str:
    """The level when it is one of the three the window offers, else the default."""
    text = str(value or "").strip()
    return text if text in AUTONOMY_LEVELS else DEFAULT_AUTONOMY


def autonomy_explanation(level: Any) -> str:
    return AUTONOMY_HELP[normalize_autonomy(level)]


def family_label(name: str) -> str:
    return FAMILY_LABELS.get(name, name)


def family_states(disabled: Optional[Iterable[str]], families: Iterable[str] = FAMILIES) -> dict[str, bool]:
    """True means the family is on. Config stores the disabled ones, so this is the inverse."""
    off = set(disabled or ())
    return {name: name not in off for name in sorted(families)}


def disabled_from_states(states: Mapping[str, bool], previous: Optional[Iterable[str]] = None,
                         families: Iterable[str] = FAMILIES) -> list[str]:
    """The disabled list to save. Names this window does not know are kept as they were."""
    known = set(families)
    kept = {name for name in (previous or ()) if name not in known}
    kept.update(name for name, on in states.items() if not on)
    return sorted(kept)


def preset_for(base_url: str, model: str = "", presets: Mapping[str, Mapping[str, str]] = PROVIDERS) -> str:
    """Name of the preset for this endpoint, preferring the one that also names this model. "" when custom."""
    base = str(base_url or "").strip().rstrip("/")
    names = [name for name, preset in presets.items() if preset["base_url"].rstrip("/") == base]
    wanted = str(model or "").strip()
    for name in names:
        if presets[name]["model"] == wanted:
            return name
    return names[0] if names else ""


def redact_line(text: str) -> str:
    """Remove secrets from one line before it is shown: bot tokens first, then cards and keys."""
    return redact_secrets(redact_tokens(str(text)))


def drain_queue(items: "queue.Queue[Any]", limit: int = 500) -> list[Any]:
    """Everything waiting in the queue, up to ``limit`` items, without blocking."""
    out: list[Any] = []
    while len(out) < limit:
        try:
            out.append(items.get_nowait())
        except queue.Empty:
            break
    return out


def unexpired(approvals: Iterable[Any], now: Optional[float] = None) -> list[Any]:
    moment = time.time() if now is None else now
    return [a for a in approvals if float(a.expires_at) > moment]


def needs_local_approval(approval: Any) -> bool:
    return bool(approval.two_channel) and not bool(approval.local_ok)


def approval_text(approval: Any) -> str:
    """Owner-facing summary of one pending approval. The frozen arguments are never shown."""
    expires = time.strftime("%H:%M", time.localtime(float(approval.expires_at)))
    return redact_line(f"{approval.summary or approval.tool} · muddati {expires}")


def approval_channel(approval: Any) -> str:
    if not approval.two_channel:
        return "Telegram'da «Ha» tugmasini bosing"
    if approval.local_ok:
        return "Mahalliy tasdiq berildi. Endi Telegram'da «Ha» tugmasini bosing."
    return "Ikki kanalli tasdiq: avval mahalliy tasdiq kerak."


def format_audit_row(row: Mapping[str, Any]) -> str:
    """One audit line: time, tool, tier, decision and code, outcome, then a short argument summary."""
    stamp = time.strftime("%d.%m %H:%M:%S", time.localtime(float(row.get("ts") or 0)))
    summary = " ".join(str(row.get("args_summary") or "").split())[:60]
    text = (f"{stamp}  {row.get('tool') or '?'}  {row.get('tier') or ''}  "
            f"{row.get('decision') or ''} {row.get('code') or ''}  {_outcome(row)}")
    if summary:
        text += f"  [{summary}]"
    return redact_line(text)


def _outcome(row: Mapping[str, Any]) -> str:
    ok = row.get("outcome_ok")
    if ok is None:
        return "natija yo'q"
    if ok:
        return "bajarildi"
    return f"xato: {row.get('outcome_code') or '?'}"


def verify_text(result: tuple[bool, Optional[int]]) -> str:
    ok, bad_id = result
    if ok:
        return "Zanjir buzilmagan: barcha yozuvlar ishonchli."
    return f"Zanjir buzilgan: qator #{bad_id if bad_id is not None else '?'} da nomuvofiqlik."


def status_line(state: str, detail: str = "", panic: bool = False) -> tuple[str, str]:
    """Colour and text for the header. The online detail is not shown: it carries a pairing code."""
    if panic:
        return BAD, "PANIC: hamma amallar to'xtatilgan"
    colour, label = STATE_STYLE.get(state, (MUTED, state))
    if state == "offline" and detail:
        return colour, redact_line(f"{label} — {detail}")
    if state == "working" and detail:
        return colour, redact_line(f"{label}: {detail}")
    return colour, label


def connect_command(code: str) -> str:
    return f"/connect {code}"


def owner_line(owner: Any) -> str:
    return "Telefon ulangan" if owner else "Hali telefon ulanmagan"


def secret_hint(present: bool) -> str:
    """Says whether a secret is stored. Never says what it is."""
    return "holat: saqlangan (yangisini kiritsangiz almashadi)" if present else "holat: kiritilmagan"


def secret_present(name: str) -> bool:
    return bool(get_secret(name))


# ------------------------------------------------------------ log handler

class QueueLogHandler(logging.Handler):
    """Hands each log line to the window through a queue, already redacted. Safe from any thread."""

    def __init__(self, maxsize: int = 1000) -> None:
        super().__init__(level=logging.INFO)
        self.lines: queue.Queue[str] = queue.Queue(maxsize=maxsize)
        self.setFormatter(logging.Formatter(LOG_FORMAT, "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = redact_line(self.format(record))
        except Exception:
            line = "(jurnal yozuvi o'qilmadi)"
        try:
            self.lines.put_nowait(line)
        except queue.Full:
            pass  # the journal keeps recent lines; dropping a burst of noise is fine


# ------------------------------------------------------------ widget helpers

def label_title(parent: tk.Widget, text: str) -> tk.Label:
    """A small uppercase section title. Packs itself."""
    label = tk.Label(parent, text=text.upper(), bg=BG, fg=MUTED, font=("Segoe UI", 8, "bold"))
    label.pack(anchor="w", pady=(14, 4))
    return label


def muted(parent: tk.Widget, text: str, bg: str = BG, size: int = 8) -> tk.Label:
    return tk.Label(parent, text=text, bg=bg, fg=MUTED, font=("Segoe UI", size), justify="left")


def entry(parent: tk.Widget, var: tk.Variable, secret: bool = False) -> tk.Entry:
    """A text field that packs itself. Secret fields show dots."""
    field = tk.Entry(parent, textvariable=var, bg=CARD, fg=FG, borderwidth=0,
                     insertbackground=FG, font=("Consolas", 10), show="•" if secret else "")
    field.pack(fill="x", ipady=6)
    return field


def checkbox(parent: tk.Widget, text: str, var: tk.BooleanVar, command: Any = None,
             bg: str = BG) -> tk.Checkbutton:
    return tk.Checkbutton(parent, text=text, variable=var, command=command, bg=bg, fg=FG,
                          selectcolor=CARD, activebackground=bg, activeforeground=FG,
                          font=("Segoe UI", 9), borderwidth=0, highlightthickness=0)


_BUTTON_STYLES = {
    "primary": (ACCENT, "#0b0d12"),
    "quiet": (CARD, FG),
    "warn": (WARN, "#0b0d12"),
    "danger": (BAD, "#0b0d12"),
}


def button(parent: tk.Widget, text: str, command: Any, kind: str = "primary") -> tk.Button:
    colour, fg = _BUTTON_STYLES[kind]
    return tk.Button(parent, text=text, command=command, bg=colour, fg=fg,
                     activebackground=colour, activeforeground=fg,
                     font=("Segoe UI Semibold", 9), borderwidth=0, padx=14, pady=6, cursor="hand2")


def _store_secret(name: str, value: str, problems: list[str]) -> bool:
    try:
        set_secret(name, value)
    except SecretStoreError as exc:  # the message names the secret, never its value
        problems.append(str(exc))
        return False
    return True


# ----------------------------------------------------------------- panels

class LogPanel:
    """Read-only journal. Lines arrive through the handler's queue and are drawn only on the Tk thread."""

    MAX_LINES = 600

    def __init__(self, parent: tk.Widget, handler: QueueLogHandler) -> None:
        self._handler = handler
        self.frame = tk.Frame(parent, bg=BG)
        self._box = tk.Text(self.frame, bg=CARD, fg=MUTED, borderwidth=0, wrap="word",
                            font=("Consolas", 9), insertbackground=FG, state="disabled")
        self._box.pack(fill="both", expand=True, padx=12, pady=12)

    def pump(self) -> None:
        lines = drain_queue(self._handler.lines)
        if not lines:
            return
        self._box.configure(state="normal")
        for line in lines:
            self._box.insert("end", str(line).rstrip() + "\n")
        total = int(self._box.index("end-1c").split(".")[0])
        if total > self.MAX_LINES:
            self._box.delete("1.0", f"{total - self.MAX_LINES + 1}.0")
        self._box.see("end")
        self._box.configure(state="disabled")


class SettingsPanel:
    """Every setting on one scrollable page. Nothing is written until Save; secrets go to the keyring only."""

    ENGINES = ("auto", "faster-whisper", "vosk", "off")

    def __init__(self, parent: tk.Widget, cfg: Config) -> None:
        self.cfg = cfg
        self.frame = tk.Frame(parent, bg=BG)
        canvas = tk.Canvas(self.frame, bg=BG, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(self.frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(canvas, bg=BG)
        item = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(item, width=e.width))
        self._build(body)

    def _build(self, body: tk.Frame) -> None:
        cfg = self.cfg
        label_title(body, "Telegram bot tokeni")
        self.token_var = tk.StringVar()
        entry(body, self.token_var, secret=True)
        self.token_hint = muted(body, "")
        self.token_hint.pack(anchor="w", pady=(2, 0))
        muted(body, "BotFather bergan token. Kalit saqlagichda turadi, faylga yozilmaydi.").pack(anchor="w")

        label_title(body, "AI provayderi")
        row = tk.Frame(body, bg=BG)
        row.pack(fill="x")
        self.preset_var = tk.StringVar(value=preset_for(cfg.get("llm_base_url"), cfg.get("llm_model")))
        combo = ttk.Combobox(row, values=list(PROVIDERS), textvariable=self.preset_var,
                             state="readonly", width=22)
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", self._on_preset)
        self.preset_hint = muted(row, PROVIDERS.get(self.preset_var.get(), {}).get("hint", "Qo'lda kiritilgan manzil"))
        self.preset_hint.pack(side="left", padx=10)
        muted(body, "Model nomi").pack(anchor="w", pady=(8, 2))
        self.model_var = tk.StringVar(value=str(cfg.get("llm_model") or ""))
        entry(body, self.model_var)

        label_title(body, "AI API kaliti")
        self.key_var = tk.StringVar()
        entry(body, self.key_var, secret=True)
        self.key_hint = muted(body, "")
        self.key_hint.pack(anchor="w", pady=(2, 0))
        muted(body, "Provayder saytidan olinadi. Kalit saqlagichda turadi.").pack(anchor="w")

        label_title(body, "Ko'rish modeli")
        self.vision_var = tk.StringVar(value=str(cfg.get("vision_model") or DEFAULTS["vision_model"]))
        entry(body, self.vision_var)
        muted(body, "Ekranni o'qish uchun. O'zgarsa, qayta ishga tushiring.").pack(anchor="w", pady=(2, 0))

        label_title(body, "Ovozli xabarlar")
        self.stt_on = tk.BooleanVar(value=bool(cfg.get("stt_enabled", True)))
        checkbox(body, "Yoqilgan", self.stt_on).pack(anchor="w")
        row2 = tk.Frame(body, bg=BG)
        row2.pack(fill="x", pady=4)
        self.stt_engine = tk.StringVar(value=str(cfg.get("stt_engine", "auto")))
        ttk.Combobox(row2, values=list(self.ENGINES), textvariable=self.stt_engine,
                     state="readonly", width=16).pack(side="left")
        muted(row2, "Dvigatel o'zgarsa, qayta ishga tushiring.").pack(side="left", padx=10)

        label_title(body, "Ishga tushish")
        self.autostart_var = tk.BooleanVar(value=tray_mod.autostart_enabled())
        checkbox(body, "Kompyuter yonganda o'zi ishga tushsin", self.autostart_var).pack(anchor="w")
        muted(body, "Oyna yopilsa ilova tray'da ishlashda davom etadi.").pack(anchor="w", pady=(2, 0))

        actions = tk.Frame(body, bg=BG)
        actions.pack(fill="x", pady=(16, 4))
        button(actions, "Saqlash", self._save).pack(side="left")
        self.result = muted(body, "", size=9)
        self.result.pack(anchor="w", pady=(6, 16))
        self.refresh()

    def refresh(self) -> None:
        self.token_hint.configure(text=secret_hint(secret_present(TOKEN_SECRET)))
        self.key_hint.configure(text=secret_hint(secret_present(LLM_KEY_SECRET)))

    def _on_preset(self, _event: Any = None) -> None:
        preset = PROVIDERS.get(self.preset_var.get())
        if preset:
            self.model_var.set(preset["model"])
            self.preset_hint.configure(text=preset["hint"])

    def _save(self) -> None:
        self.result.configure(text=self.save())

    def save(self) -> str:
        """Write every setting and return the message to show.

        A secret that cannot be stored is named in the message, never shown. Settings
        that the running assistant reads only at start-up are listed as needing a restart.
        """
        cfg = self.cfg
        restart: list[str] = []
        problems: list[str] = []

        token = self.token_var.get().strip()
        if token and _store_secret(TOKEN_SECRET, token, problems):
            restart.append("Telegram tokeni")
        key = self.key_var.get().strip()
        if key and _store_secret(LLM_KEY_SECRET, key, problems):
            restart.append("AI kaliti")

        preset = PROVIDERS.get(self.preset_var.get())
        model = self.model_var.get().strip() or (preset["model"] if preset else "")
        base_url = preset["base_url"] if preset else str(cfg.get("llm_base_url") or "")
        if base_url != cfg.get("llm_base_url") or model != cfg.get("llm_model"):
            restart.append("AI modeli")
        cfg.set("llm_base_url", base_url)
        cfg.set("llm_model", model)

        vision = self.vision_var.get().strip() or DEFAULTS["vision_model"]
        if vision != cfg.get("vision_model"):
            restart.append("ko'rish modeli")
        cfg.set("vision_model", vision)
        cfg.set("llm_vision_model", vision)  # the provider adapter reads this key

        engine = self.stt_engine.get()
        if engine != cfg.get("stt_engine"):
            restart.append("ovozli xabar dvigateli")
        cfg.set("stt_enabled", bool(self.stt_on.get()))  # read per message, so no restart
        cfg.set("stt_engine", engine)

        applied = True
        want = bool(self.autostart_var.get())
        if want != tray_mod.autostart_enabled():
            applied, message = tray_mod.set_autostart(want)
            if not applied:
                problems.append(message)
        if applied:
            cfg.set("autostart", want)

        self.token_var.set("")
        self.key_var.set("")
        self.refresh()
        text = "Saqlandi." if not problems else "Qisman saqlandi. Xato: " + "; ".join(problems)
        if restart:
            text += " Qayta ishga tushirgandan keyin qo'llanadi: " + ", ".join(restart) + "."
        return text
