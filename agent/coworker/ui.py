"""The desktop window: a header, the emergency row and five tabs.

Tabs: Ulanish (pairing and pending approvals), Rejim (autonomy and tool families),
Audit, Sozlamalar (settings) and Jurnal (the log).

Threads. The runtime runs on its own thread (asyncio.run(runtime.run())). Its status
callback, the log handler and the tray only put items on queues. The Tk thread
drains them every DRAIN_MS and makes every widget change, so no Tk call happens on
any other thread. The runtime is imported lazily so importing this module opens no
window and loads no assistant code.
"""
from __future__ import annotations

import asyncio
import logging
import os
import queue
import signal
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from . import tray as tray_mod
from .config import Config
from .tools.registry import FAMILIES
from .transport.bot import install_redaction
from .ui_panels import (
    ACCENT, AUTONOMY_LABELS, AUTONOMY_LEVELS, BAD, BG, CARD, FG, MUTED, OK, WARN,
    LogPanel, QueueLogHandler, SettingsPanel,
    approval_channel, approval_text, autonomy_explanation, button, checkbox,
    connect_command, disabled_from_states, drain_queue, family_label, family_states,
    format_audit_row, label_title, muted, needs_local_approval, normalize_autonomy,
    owner_line, status_line, unexpired, verify_text,
)

log = logging.getLogger("ui")

DRAIN_MS = 200
LIVE_EVERY = 15          # ticks between refreshes of approvals and panic state (about 3 s)
MAX_APPROVALS = 10
MUTEX_NAME = "Local\\CoworkerDesktopAgent"
ERROR_ALREADY_EXISTS = 183
_mutex_handle: Any = None


class EventBus:
    """Thread-safe queue of work for the Tk thread: status changes and tray requests."""

    def __init__(self) -> None:
        self._items: queue.Queue[tuple[str, Any]] = queue.Queue()  # unbounded: no status may be lost

    def status(self, state: str, detail: str) -> None:
        """The runtime's status callback. Called from any thread."""
        self._items.put(("status", (str(state), str(detail))))

    def post(self, kind: str, payload: Any = None) -> None:
        self._items.put((kind, payload))

    def drain(self) -> list[tuple[str, Any]]:
        return drain_queue(self._items)


# ------------------------------------------------------------ process level

def claim_single_instance() -> bool:
    """True when this is the only running copy. The mutex handle lives as long as the process."""
    global _mutex_handle
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateMutexW
    create.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    create.restype = wintypes.HANDLE
    handle = create(None, False, MUTEX_NAME)
    if not handle:
        log.warning("the single-instance mutex could not be created; starting anyway")
        return True
    _mutex_handle = handle
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def notify_already_running() -> None:
    _message("Coworker", "Coworker allaqachon ishlayapti. Tray belgisidan oynani oching.")


def show_fatal(text: str) -> None:
    _message("Coworker", text, error=True)


def _message(title: str, text: str, error: bool = False) -> None:
    root = tk.Tk()
    root.withdraw()
    try:
        (messagebox.showerror if error else messagebox.showinfo)(title, text, parent=root)
    finally:
        root.destroy()


# ------------------------------------------------------------------ window

class AgentWindow:
    def __init__(self, cfg: Config) -> None:
        from .runtime import Runtime  # deferred: the runtime pulls in the whole assistant

        self.cfg = cfg
        self.bus = EventBus()
        self.log_handler = QueueLogHandler()
        root_logger = logging.getLogger()
        root_logger.addHandler(self.log_handler)
        root_logger.setLevel(logging.INFO)
        install_redaction()
        self.runtime: Any = Runtime(cfg, status=self.bus.status)
        self.tray = tray_mod.Tray(self.runtime, on_show=lambda: self.bus.post("show"))
        self.has_tray = False
        self._told_about_tray = False
        self._code = ""
        self._code_synced = False  # True once a code was issued after the runtime started
        self._state, self._detail = "starting", ""
        self._approval_sig: Any = None
        self._ticks = 0

        self.root = tk.Tk()
        self.root.title("Coworker")
        self.root.geometry("600x740")
        self.root.minsize(540, 600)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build()
        self.root.after(DRAIN_MS, self._tick)

    # ------------------------------------------------------------------ view

    def _build(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=CARD, foreground=MUTED, padding=(14, 7))
        style.map("TNotebook.Tab", background=[("selected", BG)], foreground=[("selected", FG)])

        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=20, pady=(16, 8))
        tk.Label(head, text=str(self.cfg.get("name") or "Coworker"), bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 20)).pack(anchor="w")
        tk.Label(head, text="Coworker · kompyuter yordamchisi", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w")

        card = tk.Frame(self.root, bg=CARD)
        card.pack(fill="x", padx=20, pady=(0, 4))
        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=16, pady=10)
        self.dot = tk.Label(row, text="●", bg=CARD, fg=WARN, font=("Segoe UI", 14))
        self.dot.pack(side="left", padx=(0, 8))
        self.status = tk.Label(row, text="Ishga tushmoqda...", bg=CARD, fg=FG,
                               font=("Segoe UI Semibold", 11))
        self.status.pack(side="left")
        self.note = muted(self.root, "", size=9)
        self.note.pack(anchor="w", padx=22, pady=(0, 6))

        self._build_emergency()

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=20, pady=(0, 14))
        self.notebook.add(self._tab_link(), text="  Ulanish  ")
        self.notebook.add(self._tab_policy(), text="  Rejim  ")
        self.notebook.add(self._tab_audit(), text="  Audit  ")
        self.notebook.add(self._tab_settings(), text="  Sozlamalar  ")
        self.notebook.add(self._tab_log(), text="  Jurnal  ")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab)
        self._refresh_live()

    def _build_emergency(self) -> None:
        box = tk.Frame(self.root, bg=CARD)
        box.pack(fill="x", padx=20, pady=(0, 6))
        inner = tk.Frame(box, bg=CARD)
        inner.pack(fill="x", padx=14, pady=(10, 4))
        button(inner, "STOP", self._stop, kind="warn").pack(side="left")
        button(inner, "PANIC", self._panic, kind="danger").pack(side="left", padx=8)
        button(inner, "Qayta yoqish (faqat shu kompyuterdan)", self._resume, kind="quiet").pack(side="left")
        self.panic_label = tk.Label(inner, text="", bg=CARD, fg=BAD, font=("Segoe UI Semibold", 9))
        self.panic_label.pack(side="right")
        tk.Label(box, text=("STOP: hozirgi ishlar bekor qilinadi. PANIC: hamma amallar to'xtaydi. "
                            "Qayta yoqish Telegram orqali emas, faqat shu oynadan mumkin."),
                 bg=CARD, fg=MUTED, font=("Segoe UI", 8), wraplength=520,
                 justify="left").pack(anchor="w", padx=14, pady=(0, 10))

    def _tab_link(self) -> tk.Frame:
        f = tk.Frame(self.notebook, bg=BG)
        label_title(f, "Telefonni ulash")
        card = tk.Frame(f, bg=CARD)
        card.pack(fill="x")
        muted(card, "Telegram botingizga shu buyruqni yuboring:", bg=CARD).pack(pady=(14, 4))
        self.code_label = tk.Label(card, text="—", bg=CARD, fg=ACCENT, font=("Consolas", 20, "bold"))
        self.code_label.pack(pady=(0, 4))
        muted(card, "Kod 10 daqiqa amal qiladi. Yangisini olsangiz, eskisi ishlamaydi.",
              bg=CARD).pack(pady=(0, 10))
        btns = tk.Frame(card, bg=CARD)
        btns.pack(pady=(0, 12))
        button(btns, "Nusxalash", self._copy_code).pack(side="left")
        button(btns, "Yangi kod", self._new_code, kind="quiet").pack(side="left", padx=8)
        self.owner_label = muted(f, "", size=9)
        self.owner_label.pack(anchor="w", pady=(8, 0))

        label_title(f, "Kutilayotgan tasdiqlar")
        self.approval_box = tk.Frame(f, bg=BG)
        self.approval_box.pack(fill="x")
        return f

    def _tab_policy(self) -> tk.Frame:
        f = tk.Frame(self.notebook, bg=BG)
        label_title(f, "Avtonomiya")
        self.autonomy_var = tk.StringVar(value=normalize_autonomy(self.cfg.get("autonomy")))
        for level in AUTONOMY_LEVELS:
            row = tk.Frame(f, bg=CARD)
            row.pack(fill="x", pady=3)
            tk.Radiobutton(row, text=AUTONOMY_LABELS[level], variable=self.autonomy_var, value=level,
                           command=self._set_autonomy, bg=CARD, fg=FG, selectcolor=BG,
                           activebackground=CARD, activeforeground=FG, font=("Segoe UI Semibold", 9),
                           borderwidth=0, highlightthickness=0, anchor="w").pack(anchor="w", padx=12, pady=(8, 0))
            tk.Label(row, text=autonomy_explanation(level), bg=CARD, fg=MUTED, font=("Segoe UI", 8),
                     wraplength=500, justify="left").pack(anchor="w", padx=34, pady=(0, 8))

        label_title(f, "Bo'limlar")
        muted(f, "Belgisi olib tashlangan bo'lim o'chiriladi. Har bir amal oldin tekshiriladi.").pack(anchor="w")
        grid = tk.Frame(f, bg=BG)
        grid.pack(fill="x", pady=(6, 0))
        states = family_states(self.cfg.get("disabled_families", []) or [])
        self.family_vars: dict[str, tk.BooleanVar] = {}
        for index, name in enumerate(sorted(FAMILIES)):
            var = tk.BooleanVar(value=states[name])
            self.family_vars[name] = var
            checkbox(grid, family_label(name), var, command=self._save_families).grid(
                row=index // 2, column=index % 2, sticky="w", padx=(0, 16), pady=2)
        return f

    def _tab_audit(self) -> tk.Frame:
        f = tk.Frame(self.notebook, bg=BG)
        self._audit_tab = f
        self.audit_box = tk.Text(f, bg=CARD, fg=MUTED, borderwidth=0, wrap="word", font=("Consolas", 9),
                                 height=18, state="disabled")
        self.audit_box.pack(fill="both", expand=True, pady=(12, 8))
        row = tk.Frame(f, bg=BG)
        row.pack(fill="x")
        button(row, "Yangilash", self._refresh_audit, kind="quiet").pack(side="left")
        button(row, "Zanjirni tekshirish", self._verify_audit).pack(side="left", padx=8)
        self.audit_result = muted(f, "", size=9)
        self.audit_result.pack(anchor="w", pady=(8, 0))
        return f

    def _tab_settings(self) -> tk.Frame:
        self.settings = SettingsPanel(self.notebook, self.cfg)
        return self.settings.frame

    def _tab_log(self) -> tk.Frame:
        self.log_panel = LogPanel(self.notebook, self.log_handler)
        return self.log_panel.frame

    # --------------------------------------------------------------- actions

    def _say(self, text: str) -> None:
        self.note.configure(text=text)

    def _set_autonomy(self) -> None:
        level = normalize_autonomy(self.autonomy_var.get())
        self.runtime.set_autonomy(level)
        self._say(f"Avtonomiya saqlandi: {AUTONOMY_LABELS[level]}.")

    def _save_families(self) -> None:
        states = {name: var.get() for name, var in self.family_vars.items()}
        previous = self.cfg.get("disabled_families", []) or []
        self.cfg.set("disabled_families", disabled_from_states(states, previous))
        self._say("Bo'limlar saqlandi.")

    def _stop(self) -> None:
        self.runtime.kill.stop()
        self._say("To'xtatildi. Hozirgi ishlar bekor qilindi.")
        self._refresh_live()

    def _panic(self) -> None:
        question = ("Hamma amallar darhol to'xtatiladi. Qayta yoqish faqat shu kompyuterdan mumkin. "
                    "Davom etasizmi?")
        if not messagebox.askyesno("PANIC", question, icon="warning", parent=self.root):
            return
        self.runtime.kill.panic()
        self._say("PANIC yoqildi. Hamma amallar to'xtatildi.")
        self._refresh_live()

    def _resume(self) -> None:
        if not self._panic_on():
            self._say("PANIC yoqilmagan edi.")
            return
        question = "PANIC o'chiriladi va agent yana ishlay boshlaydi. Davom etasizmi?"
        if not messagebox.askyesno("Qayta yoqish", question, icon="question", parent=self.root):
            return
        resumed = self.runtime.resume_locally()
        self._say("PANIC o'chirildi." if resumed else "PANIC o'chirilmadi.")
        self._refresh_live()

    def _new_code(self) -> None:
        # An explicit request from the desktop is the owner's own act, so it also clears
        # a lockout left by wrong guesses. Telegram cannot do this.
        self.runtime.pairing.clear_lockout()
        self._issue_code()
        self._say("Yangi kod tayyor. Eski kod endi ishlamaydi.")

    def _issue_code(self) -> None:
        self._code = self.runtime.pairing.issue_code()
        self.code_label.configure(text=connect_command(self._code))

    def _copy_code(self) -> None:
        if not self._code:
            self._say("Kod hali tayyor emas. Bir oz kuting.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(connect_command(self._code))
        self._say("Nusxalandi. Telegram botiga yuboring.")

    def _local_approve(self, approval: Any) -> None:
        question = (f"{approval_text(approval)}\n\nBu amal ikki kanal orqali tasdiqlanadi. "
                    "Mahalliy tasdiqdan keyin Telegram'da ham «Ha» bosilishi kerak. Davom etasizmi?")
        if not messagebox.askyesno("Mahalliy tasdiq", question, parent=self.root):
            return
        if self.runtime.approvals.local_approve(approval.id):
            self._say("Mahalliy tasdiq berildi. Endi Telegram'da «Ha» bosing.")
        else:
            self._say("Tasdiq muddati tugagan yoki allaqachon yopilgan.")
        self._approval_sig = None
        self._refresh_approvals(self._owner())

    def _on_tab(self, _event: Any = None) -> None:
        if str(self.notebook.select()) == str(self._audit_tab):
            self._refresh_audit()

    def _refresh_audit(self) -> None:
        rows = self.runtime.store.audit_tail(30)
        text = "\n".join(format_audit_row(row) for row in rows) or "Hozircha amallar yo'q."
        self.audit_box.configure(state="normal")
        self.audit_box.delete("1.0", "end")
        self.audit_box.insert("1.0", text)
        self.audit_box.configure(state="disabled")

    def _verify_audit(self) -> None:
        result = self.runtime.store.audit_verify()
        self.audit_result.configure(text=verify_text(result), fg=OK if result[0] else BAD)

    # ------------------------------------------------------------- refresh

    def _owner(self) -> Any:
        return self.runtime.pairing.owner()

    def _panic_on(self) -> bool:
        return bool(self.runtime.kill.is_panic())

    def _refresh_live(self) -> None:
        self.panic_label.configure(text="PANIC YOQILGAN" if self._panic_on() else "")
        owner = self._owner()
        self.owner_label.configure(text=owner_line(owner))
        self._refresh_approvals(owner)
        self._show_status()

    def _refresh_approvals(self, owner: Any) -> None:
        items: list[Any] = []
        if owner is not None:
            items = unexpired(self.runtime.store.approvals_pending(owner[1]))
        sig = (owner is not None, tuple((a.id, bool(a.local_ok)) for a in items))
        if sig == self._approval_sig:
            return
        self._approval_sig = sig
        for child in self.approval_box.winfo_children():
            child.destroy()
        if owner is None:
            muted(self.approval_box, "Telefon ulanmagan, shuning uchun tasdiqlar yo'q.").pack(anchor="w", pady=6)
            return
        if not items:
            muted(self.approval_box, "Kutilayotgan tasdiq yo'q.").pack(anchor="w", pady=6)
            return
        for approval in items[:MAX_APPROVALS]:
            self._approval_row(approval)
        if len(items) > MAX_APPROVALS:
            muted(self.approval_box, f"Yana {len(items) - MAX_APPROVALS} ta tasdiq bor.").pack(anchor="w")

    def _approval_row(self, approval: Any) -> None:
        row = tk.Frame(self.approval_box, bg=CARD)
        row.pack(fill="x", pady=3)
        text = tk.Frame(row, bg=CARD)
        text.pack(side="left", fill="x", expand=True, padx=12, pady=8)
        tk.Label(text, text=approval_text(approval), bg=CARD, fg=FG, font=("Segoe UI", 9),
                 wraplength=380, justify="left").pack(anchor="w")
        tk.Label(text, text=approval_channel(approval), bg=CARD, fg=MUTED, font=("Segoe UI", 8),
                 wraplength=380, justify="left").pack(anchor="w")
        if approval.two_channel:
            local = button(row, "Mahalliy tasdiq", lambda a=approval: self._local_approve(a))
            if not needs_local_approval(approval):
                local.configure(text="Mahalliy tasdiq berilgan", state="disabled")
            local.pack(side="right", padx=10)

    def _show_status(self) -> None:
        colour, text = status_line(self._state, self._detail, panic=self._panic_on())
        self.dot.configure(fg=colour)
        self.status.configure(text=text)
        self.tray.update(self._state, text)

    def _on_status(self, state: str, detail: str) -> None:
        self._state, self._detail = state, detail
        if state == "online" and not self._code_synced:
            # The runtime has just issued its own pairing code, which is now stale.
            # Issue one more so the code on screen is the one that works.
            self._code_synced = True
            self._issue_code()
        self._show_status()

    def _tick(self) -> None:
        try:
            for kind, payload in self.bus.drain():
                if kind == "status":
                    self._on_status(*payload)
                elif kind == "show":
                    self._raise_window()
            self.log_panel.pump()
            self._ticks += 1
            if self._ticks % LIVE_EVERY == 0:
                self._refresh_live()
        except Exception:
            log.exception("the window could not refresh")
        finally:
            self.root.after(DRAIN_MS, self._tick)

    # ----------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Start the tray, the runtime thread and the Tk main loop."""
        tray_mod.sync_autostart(bool(self.cfg.get("autostart")))
        self.has_tray = self.tray.start()
        if not self.has_tray:
            log.warning("Tray ishlamadi — oyna yopilsa ilova to'xtaydi. Tuzatish: pip install pystray pillow")
        threading.Thread(target=self._run_runtime, name="runtime", daemon=True).start()
        # Ctrl+C that lands while Tk is busy can be swallowed by a Tk callback, so the
        # signal is handled directly and the process quits for real.
        try:
            signal.signal(signal.SIGINT, lambda *_: self.tray.quit())
        except (ValueError, OSError):
            pass
        try:
            self.root.mainloop()
        finally:
            self.tray.quit()

    def _run_runtime(self) -> None:
        try:
            asyncio.run(self.runtime.run())
        except Exception:
            log.exception("the assistant stopped with an error")
            self.bus.status("offline", "xato bilan to'xtadi")

    def _on_close(self) -> None:
        """The X button hides the window. The agent keeps running in the tray."""
        if not self.has_tray:
            self.tray.quit()
            return
        self.root.withdraw()
        if not self._told_about_tray:
            self._told_about_tray = True
            self.tray.notify("Coworker fonda ishlashda davom etmoqda. "
                             "Butunlay chiqish uchun tray belgisidan «Chiqish» ni tanlang.")

    def _raise_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
