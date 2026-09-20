"""Tkinter control panel for the desktop agent.

Tkinter ships with Python, so the packaged app stays small and needs no extra
runtime. The asyncio side runs on its own thread; every UI update is marshalled
back through ``root.after`` because Tk is not thread-safe.
"""
from __future__ import annotations

import asyncio
import os
import queue
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from . import tray as tray_mod
from .app import CoworkerAgent
from .config import ALL_CAPS, CAP_FIND, CAP_LABELS, Config
from .llm import PROVIDERS

BG = "#12151c"
CARD = "#1a1f2a"
FG = "#e8ecf3"
MUTED = "#8a94a6"
ACCENT = "#4a9eff"
OK = "#3ddc84"
WARN = "#ffb020"
BAD = "#ff5c5c"

STATE_STYLE = {
    "online": (OK, "Ulangan"),
    "paired": (OK, "Telefon ulandi"),
    "working": (ACCENT, "Ishlayapti"),
    "connecting": (WARN, "Ulanmoqda"),
    "offline": (BAD, "Aloqa yo'q"),
}


class AgentWindow:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.agent: CoworkerAgent | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.tray = tray_mod.Tray(lambda: None, lambda: None)
        self.has_tray = False
        self._told_about_tray = False

        self.root = tk.Tk()
        self.root.title("Coworker")
        self.root.geometry("560x640")
        self.root.minsize(520, 560)
        self.root.configure(bg=BG)

        self._build()
        self.root.after(120, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ view

    def _build(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=CARD, foreground=MUTED, padding=(16, 8))
        style.map("TNotebook.Tab", background=[("selected", BG)], foreground=[("selected", FG)])
        style.configure("TFrame", background=BG)

        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=20, pady=(18, 10))
        tk.Label(header, text="Coworker", bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 20)).pack(anchor="w")
        tk.Label(header, text=f"{self.cfg.get('name')} · hujjat yordamchisi",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")

        # Status pill
        status_card = tk.Frame(self.root, bg=CARD)
        status_card.pack(fill="x", padx=20, pady=(0, 12))
        inner = tk.Frame(status_card, bg=CARD)
        inner.pack(fill="x", padx=16, pady=12)
        self.dot = tk.Label(inner, text="●", bg=CARD, fg=BAD, font=("Segoe UI", 14))
        self.dot.pack(side="left", padx=(0, 8))
        self.status_text = tk.Label(inner, text="Ishga tushmoqda...", bg=CARD, fg=FG,
                                    font=("Segoe UI Semibold", 11))
        self.status_text.pack(side="left")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        notebook.add(self._tab_main(notebook), text="  Ulanish  ")
        notebook.add(self._tab_settings(notebook), text="  Sozlamalar  ")
        notebook.add(self._tab_log(notebook), text="  Jurnal  ")

    def _tab_main(self, parent: ttk.Notebook) -> tk.Frame:
        f = tk.Frame(parent, bg=BG)

        tk.Label(f, text="TELEFONNI ULASH", bg=BG, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(16, 6))

        card = tk.Frame(f, bg=CARD)
        card.pack(fill="x")
        tk.Label(card, text="Telegramda @ulugcoworkerbot ga yozing:",
                 bg=CARD, fg=MUTED, font=("Segoe UI", 9)).pack(pady=(14, 4))
        self.code_label = tk.Label(card, text=f"/connect {self.cfg.pair_code}", bg=CARD,
                                   fg=ACCENT, font=("Consolas", 20, "bold"))
        self.code_label.pack(pady=(0, 6))
        tk.Label(card, text="Kod ilova yopilguncha amal qiladi", bg=CARD, fg=MUTED,
                 font=("Segoe UI", 8)).pack(pady=(0, 12))

        btns = tk.Frame(f, bg=BG)
        btns.pack(fill="x", pady=10)
        self._button(btns, "Kodni nusxalash", self._copy_code).pack(side="left")
        self._button(btns, "Botni ochish", self._open_bot, primary=False).pack(side="left", padx=8)

        tk.Label(f, text="ULANGAN TELEFONLAR", bg=BG, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(16, 6))
        self.chats_box = tk.Listbox(
            f, bg=CARD, fg=FG, borderwidth=0, highlightthickness=0,
            selectbackground=ACCENT, font=("Segoe UI", 10), height=5,
            activestyle="none",
        )
        self.chats_box.pack(fill="x")
        self.chats_box.bind("<<ListboxSelect>>", self._on_chat_select)

        # Capability is granted per phone, never inherited from being paired.
        # The father's phone should stay on "find documents" forever.
        self.caps_frame = tk.Frame(f, bg=CARD)
        self.caps_frame.pack(fill="x", pady=(10, 0))
        self.caps_title = tk.Label(
            self.caps_frame, text="RUXSATLAR", bg=CARD, fg=MUTED,
            font=("Segoe UI", 8, "bold"),
        )
        self.caps_title.pack(anchor="w", padx=12, pady=(10, 4))
        self.cap_vars: dict[str, tk.BooleanVar] = {}
        for cap in ALL_CAPS:
            var = tk.BooleanVar(value=False)
            self.cap_vars[cap] = var
            tk.Checkbutton(
                self.caps_frame, text=CAP_LABELS[cap], variable=var,
                bg=CARD, fg=FG, selectcolor=BG, activebackground=CARD,
                activeforeground=FG, font=("Segoe UI", 9), borderwidth=0,
                highlightthickness=0, state="disabled",
                command=lambda c=cap: self._toggle_cap(c),
            ).pack(anchor="w", padx=12)
        self.caps_hint = tk.Label(
            self.caps_frame, text="Telefonni tanlang", bg=CARD, fg=MUTED,
            font=("Segoe UI", 8),
        )
        self.caps_hint.pack(anchor="w", padx=12, pady=(2, 10))

        self._button(f, "Tanlanganni o'chirish", self._revoke, primary=False).pack(anchor="w", pady=8)
        self._refresh_chats()
        return f

    def _selected_chat(self) -> int | None:
        sel = self.chats_box.curselection()
        chats = self.cfg.chats
        if not sel or sel[0] >= len(chats):
            return None
        return chats[sel[0]]

    def _on_chat_select(self, _event=None) -> None:
        chat_id = self._selected_chat()
        widgets = [w for w in self.caps_frame.winfo_children() if isinstance(w, tk.Checkbutton)]
        if chat_id is None:
            for w in widgets:
                w.configure(state="disabled")
            self.caps_hint.configure(text="Telefonni tanlang")
            return

        current = self.cfg.caps(chat_id)
        for cap, var in self.cap_vars.items():
            var.set(cap in current)
        for cap, w in zip(ALL_CAPS, widgets):
            # FIND is what pairing means; it cannot be switched off separately.
            w.configure(state="disabled" if cap == CAP_FIND else "normal")
        self.caps_hint.configure(text=f"Telegram ID {chat_id}")

    def _toggle_cap(self, _cap: str) -> None:
        chat_id = self._selected_chat()
        if chat_id is None:
            return
        chosen = [c for c, v in self.cap_vars.items() if v.get()]
        self.cfg.set_caps(chat_id, chosen)
        self._log(f"{chat_id} ruxsatlari: {', '.join(self.cfg.caps(chat_id))}")

    def _tab_settings(self, parent: ttk.Notebook) -> tk.Frame:
        f = tk.Frame(parent, bg=BG)
        canvas = tk.Canvas(f, bg=BG, highlightthickness=0)
        body = tk.Frame(canvas, bg=BG)
        canvas.pack(fill="both", expand=True)
        canvas.create_window((0, 0), window=body, anchor="nw", width=470)
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        self.vars: dict[str, tk.Variable] = {}

        self._field(body, "Server manzili", "server_url",
                    "Render'dagi manzil, masalan https://coworker.onrender.com")

        tk.Label(body, text="AI MODELI", bg=BG, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(16, 4))
        row = tk.Frame(body, bg=BG)
        row.pack(fill="x", pady=(0, 6))
        self.provider = tk.StringVar(value="DeepSeek")
        combo = ttk.Combobox(row, values=list(PROVIDERS), textvariable=self.provider,
                             state="readonly", width=20)
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", self._apply_provider)
        self.provider_hint = tk.Label(row, text=PROVIDERS["DeepSeek"]["hint"],
                                      bg=BG, fg=MUTED, font=("Segoe UI", 8))
        self.provider_hint.pack(side="left", padx=10)

        self._field(body, "API kalit", "llm_api_key", "Provayder saytidan olinadi", secret=True)
        self._field(body, "Model nomi", "llm_model", "")
        self._field(body, "Server maxfiy kaliti", "relay_token",
                    "Ixtiyoriy — serverdagi RELAY_TOKEN bilan bir xil bo'lishi kerak", secret=True)

        tk.Label(body, text="QIDIRUV DOIRASI", bg=BG, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(16, 4))
        tk.Label(body, text="Har qatorda bitta papka. Bo'sh qoldirilsa — barcha disklar.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w")
        self.roots_box = tk.Text(body, height=4, bg=CARD, fg=FG, borderwidth=0,
                                 insertbackground=FG, font=("Consolas", 9))
        self.roots_box.pack(fill="x", pady=4)
        self.roots_box.insert("1.0", "\n".join(self.cfg.get("roots", [])))

        tk.Label(body, text="OVOZLI XABAR", bg=BG, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(16, 4))
        self.stt_on = tk.BooleanVar(value=bool(self.cfg.get("stt_enabled", True)))
        tk.Checkbutton(body, text="Yoqilgan", variable=self.stt_on, bg=BG, fg=FG,
                       selectcolor=CARD, activebackground=BG, activeforeground=FG,
                       font=("Segoe UI", 9), borderwidth=0,
                       highlightthickness=0).pack(anchor="w")
        srow = tk.Frame(body, bg=BG)
        srow.pack(fill="x", pady=4)
        self.stt_engine = tk.StringVar(value=self.cfg.get("stt_engine", "auto"))
        ttk.Combobox(srow, values=["auto", "faster-whisper", "vosk", "off"],
                     textvariable=self.stt_engine, state="readonly", width=16).pack(side="left")
        self.stt_status = tk.Label(srow, text="", bg=BG, fg=MUTED, font=("Segoe UI", 8))
        self.stt_status.pack(side="left", padx=10)

        tk.Label(body, text="ISHGA TUSHISH", bg=BG, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(16, 4))
        self.autostart = tk.BooleanVar(value=tray_mod.autostart_enabled())
        tk.Checkbutton(body, text="Kompyuter yonganda o'zi ishga tushsin",
                       variable=self.autostart, bg=BG, fg=FG, selectcolor=CARD,
                       activebackground=BG, activeforeground=FG,
                       font=("Segoe UI", 9), borderwidth=0,
                       highlightthickness=0).pack(anchor="w")
        tk.Label(body, text="Oyna yopilsa ilova tray'da ishlashda davom etadi.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w", pady=(2, 0))

        actions = tk.Frame(body, bg=BG)
        actions.pack(fill="x", pady=18)
        self._button(actions, "Saqlash", self._save).pack(side="left")
        self._button(actions, "AI ni tekshirish", self._test_llm, primary=False).pack(side="left", padx=8)
        return f

    def _tab_log(self, parent: ttk.Notebook) -> tk.Frame:
        f = tk.Frame(parent, bg=BG)
        self.log_box = tk.Text(f, bg=CARD, fg=MUTED, borderwidth=0, wrap="word",
                               font=("Consolas", 9), insertbackground=FG)
        self.log_box.pack(fill="both", expand=True, pady=12)
        self.log_box.configure(state="disabled")
        return f

    # --------------------------------------------------------------- widgets

    def _field(self, parent: tk.Widget, label: str, key: str, hint: str, secret: bool = False) -> None:
        tk.Label(parent, text=label.upper(), bg=BG, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(12, 4))
        var = tk.StringVar(value=str(self.cfg.get(key, "")))
        entry = tk.Entry(parent, textvariable=var, bg=CARD, fg=FG, borderwidth=0,
                         insertbackground=FG, font=("Consolas", 10),
                         show="•" if secret else "")
        entry.pack(fill="x", ipady=6)
        if hint:
            tk.Label(parent, text=hint, bg=BG, fg=MUTED,
                     font=("Segoe UI", 8)).pack(anchor="w", pady=(2, 0))
        self.vars[key] = var

    def _button(self, parent: tk.Widget, text: str, command, primary: bool = True) -> tk.Button:
        return tk.Button(
            parent, text=text, command=command,
            bg=ACCENT if primary else CARD, fg="#0b0d12" if primary else FG,
            activebackground=ACCENT if primary else CARD,
            font=("Segoe UI Semibold", 9), borderwidth=0, padx=16, pady=7,
            cursor="hand2",
        )

    # --------------------------------------------------------------- actions

    def _apply_provider(self, _event=None) -> None:
        preset = PROVIDERS.get(self.provider.get())
        if not preset:
            return
        self.vars["llm_model"].set(preset["model"])
        self.cfg.set("llm_base_url", preset["base_url"])
        self.provider_hint.configure(text=preset["hint"])

    def _save(self) -> None:
        for key, var in self.vars.items():
            self.cfg.set(key, var.get().strip())
        roots = [r.strip() for r in self.roots_box.get("1.0", "end").splitlines() if r.strip()]
        self.cfg.set("roots", roots)
        self.cfg.set("stt_enabled", bool(self.stt_on.get()))
        self.cfg.set("stt_engine", self.stt_engine.get())
        self._log("Sozlamalar saqlandi. Ilovani qayta ishga tushiring.")
        messagebox.showinfo("Coworker", "Saqlandi.\nO'zgarishlar uchun ilovani qayta oching.")

    def _test_llm(self) -> None:
        self._save()
        self._log("AI tekshirilmoqda...")

        def done(ok: bool, detail: str) -> None:
            self._log(("AI javob berdi: " if ok else "AI xatosi: ") + detail)
            (messagebox.showinfo if ok else messagebox.showerror)("Coworker", detail)

        async def probe() -> None:
            from .llm import LLM
            client = LLM(self.cfg.get("llm_base_url"), self.cfg.get("llm_api_key"),
                         self.cfg.get("llm_model"))
            ok, detail = await client.ping()
            await client.close()
            self.root.after(0, lambda: done(ok, detail))

        if self.loop:
            asyncio.run_coroutine_threadsafe(probe(), self.loop)

    def _copy_code(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(f"/connect {self.cfg.pair_code}")
        self._log("Kod nusxalandi.")

    def _open_bot(self) -> None:
        webbrowser.open("https://t.me/ulugcoworkerbot")

    def _revoke(self) -> None:
        selection = self.chats_box.curselection()
        if not selection:
            return
        chat_id = self.cfg.chats[selection[0]]
        self.cfg.revoke(chat_id)
        self._refresh_chats()
        if self.agent and self.loop:
            asyncio.run_coroutine_threadsafe(self.agent.link.push_state(), self.loop)
        self._log(f"{chat_id} o'chirildi.")

    def _refresh_chats(self) -> None:
        self.chats_box.delete(0, "end")
        for chat_id in self.cfg.chats:
            self.chats_box.insert("end", f"  Telegram ID {chat_id}")
        if not self.cfg.chats:
            self.chats_box.insert("end", "  (hali hech kim ulanmagan)")
        if hasattr(self, "caps_frame"):
            self._on_chat_select()

    # ---------------------------------------------------------------- events

    def push(self, state: str, detail: str) -> None:
        """Thread-safe entry point used by the agent."""
        self.events.put((state, detail))

    def _drain(self) -> None:
        while True:
            try:
                state, detail = self.events.get_nowait()
            except queue.Empty:
                break
            self._apply_state(state, detail)
        self.root.after(120, self._drain)

    def _apply_state(self, state: str, detail: str) -> None:
        if state == "note":
            self._log(detail)
            self._refresh_chats()
            return
        if state == "paired":
            self._refresh_chats()

        colour, label = STATE_STYLE.get(state, (MUTED, state))
        self.dot.configure(fg=colour)
        text = label
        if state == "working" and detail:
            text = f"{label}: {detail}"
        elif state == "offline" and detail:
            text = f"{label} — {detail}"
        self.status_text.configure(text=text)
        self.tray.update(state, text)
        if state in ("offline", "connecting"):
            self._log(f"{label}: {detail}")

    def _log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message.rstrip() + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        threading.Thread(target=self._run_loop, daemon=True).start()
        self.tray = tray_mod.Tray(self._show_window, self._quit)
        self.has_tray = self.tray.start()
        if not self.has_tray:
            self._log("Tray ishlamadi — oyna yopilsa ilova to'xtaydi. "
                      "Tuzatish: pip install pystray pillow")
        self.root.after(600, self._show_stt_status)
        self.root.mainloop()
        self._hard_exit()

    def _hard_exit(self) -> None:
        """pystray runs its backend on non-daemon threads, so returning from
        mainloop normally would leave this process alive and invisible - still
        holding the WebSocket and answering Telegram after the user quit.
        Give the agent a moment to close its socket, then exit for real."""
        self.tray.stop()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if self.agent is None or not self.agent.link.connected:
                break
            time.sleep(0.05)
        os._exit(0)

    def _run_loop(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.agent = CoworkerAgent(self.cfg, self.push)
        try:
            self.loop.run_until_complete(self.agent.run())
        except Exception as exc:
            self.push("offline", str(exc)[:120])

    def _show_stt_status(self) -> None:
        if self.agent:
            self.stt_status.configure(text=self.agent.stt.status)

    def _on_close(self) -> None:
        """The X button hides to the tray - the agent must keep running."""
        if not self.has_tray:
            self._quit()
            return
        self.root.withdraw()
        if not self._told_about_tray:
            self._told_about_tray = True
            self.tray.notify(
                "Coworker fonda ishlashda davom etmoqda. "
                "Butunlay chiqish uchun tray belgisiga o'ng tugma bosing."
            )

    def _show_window(self) -> None:
        """Called from the tray thread - bounce onto the Tk thread."""
        self.root.after(0, self._raise_window)

    def _raise_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _quit(self) -> None:
        if self.agent and self.loop:
            asyncio.run_coroutine_threadsafe(self.agent.stop(), self.loop)
        self.tray.stop()
        self.root.after(0, self.root.destroy)


def main() -> None:
    cfg = Config()
    AgentWindow(cfg).start()
