"""System tray icon, the hard exit, and Windows autostart.

The agent is only useful while it is running, but nobody keeps a window open
all day - they hit the X. Closing the window therefore hides it to the tray
instead of quitting, and the agent keeps serving Telegram from there.

The tray makes no policy decisions. Its menu calls runtime.kill (STOP, PANIC)
and runtime.stop (Quit). Show is a callback that the window handles on the Tk
thread. Icons are drawn with Pillow in memory, so the build needs no asset file.

Autostart is deliberately the per-user Run key rather than a Windows service:
a service runs in session 0 with no desktop, which would break the moment this
agent is ever asked to interact with the screen.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("tray")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Coworker"
STOP_TIMEOUT_S = 5.0
PANIC_TITLE = "Coworker: PANIC"
PANIC_QUESTION = ("PANIC: hamma amallar darhol to'xtatiladi. "
                  "Qayta yoqish faqat shu kompyuterdan mumkin. Davom etasizmi?")


def hard_exit(code: int = 0) -> None:
    """Flush the logs and end the process now.

    Worker threads (pystray, COM, the poll loop) can keep a normal interpreter
    shutdown waiting, so this is the one exit path that is certain to finish.
    """
    try:
        logging.shutdown()
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception:
            pass
    os._exit(code)


# ------------------------------------------------------------------ autostart

def launch_command() -> str:
    """The command Windows should run at login, quoted for the registry.

    Returns "" when the entry point cannot be determined - writing a guess
    into the Run key produces a login-time error box the user cannot trace
    back to us, which is worse than not offering autostart at all.
    """
    if getattr(sys, "frozen", False):          # PyInstaller build
        return f'"{sys.executable}"'

    # run.py sits one level above this package; derive it from __file__ rather
    # than argv[0], which is unreliable (relative paths, -c, -m, frozen shims).
    script = Path(__file__).resolve().parent.parent / "run.py"
    if not script.is_file():
        fallback = Path(sys.argv[0]).resolve()
        if fallback.suffix.lower() != ".py" or not fallback.is_file():
            log.warning("autostart: run.py was not found, so no launch command is known")
            return ""
        script = fallback

    # pythonw.exe keeps a console window from flashing up on every login.
    exe = Path(sys.executable)
    quiet = exe.with_name("pythonw.exe")
    runner = quiet if quiet.exists() else exe
    return f'"{runner}" "{script}"'


def stored_command() -> str:
    """The command the Run key holds now, or "" when there is none or it cannot be read."""
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_NAME)
            return str(value or "")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        log.warning("autostart: the Run key could not be read: %s", exc)
        return ""


def autostart_enabled() -> bool:
    return bool(stored_command())


def set_autostart(enabled: bool) -> tuple[bool, str]:
    """Returns (ok, message). Never raises - this is a convenience, not core."""
    if os.name != "nt":
        return False, "Autostart faqat Windows'da"
    try:
        import winreg

        command = launch_command() if enabled else ""
        if enabled and not command:
            return False, "Ilova yo'li aniqlanmadi — autostart o'rnatilmadi"

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command)
                log.info("autostart enabled")
                return True, "Kompyuter yonganda avtomatik ishga tushadi"
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass
            log.info("autostart disabled")
            return True, "Avtomatik ishga tushish o'chirildi"
    except OSError as exc:
        log.warning("autostart failed: %s", exc)
        return False, f"Xato: {exc}"


def sync_autostart(want: bool) -> bool:
    """Make the Run key match a saved "on" choice.

    The stored command goes stale when the folder moves or Python is replaced,
    so it is compared with the current one and rewritten when they differ.
    Returns False when the key could not be brought up to date (already logged).
    """
    if not want or os.name != "nt":
        return True
    current = launch_command()
    if not current:
        return False
    if stored_command() == current:
        return True
    ok, message = set_autostart(True)
    if not ok:
        log.warning("autostart could not be refreshed: %s", message)
    return ok


# ----------------------------------------------------------------- tray icon

def available() -> bool:
    import importlib.util

    return bool(
        importlib.util.find_spec("pystray") and importlib.util.find_spec("PIL")
    )


def _icon_image(colour: str, panic: bool = False):
    """Draw the icon in memory so the build needs no asset file.

    A document sheet tinted by connection state. In panic the sheet carries a
    white exclamation mark instead of text lines, so the state shows at a glance.
    """
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([12, 6, 52, 58], radius=6, fill=colour)
    d.polygon([(38, 6), (52, 20), (38, 20)], fill="#0b0d12")
    if panic:
        d.rectangle([30, 24, 34, 42], fill="#ffffff")
        d.rectangle([30, 46, 34, 50], fill="#ffffff")
    else:
        for y in (30, 38, 46):
            d.line([20, y, 44, y], fill="#0b0d12", width=3)
    return img


def _confirm(title: str, text: str) -> bool:
    """A native yes/no box. Used from the pystray thread, where Tk must not be called.

    If the box cannot be shown, the request goes ahead: PANIC is a stop, not a
    delete, and the owner asked for it.
    """
    if os.name != "nt":
        return True
    try:
        import ctypes

        mb_yesno_warning_topmost = 0x40034
        idyes = 6
        answer = ctypes.windll.user32.MessageBoxW(None, text, title, mb_yesno_warning_topmost)
        return answer == idyes
    except Exception as exc:
        log.warning("confirmation box unavailable: %s", exc)
        return True


class Tray:
    """pystray wrapper. Menu callbacks run on the pystray thread and never touch Tk."""

    COLOURS = {"online": "#3ddc84", "working": "#4a9eff", "offline": "#ff5c5c"}
    PANIC_COLOUR = "#b00020"

    def __init__(self, runtime: Any, on_show: Callable[[], None], title: str = "Coworker") -> None:
        self._runtime = runtime
        self._on_show = on_show
        self._title = title
        self._icon: Any = None
        self._state = "offline"
        self._tooltip = ""
        self._shown: tuple | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> bool:
        if not available():
            log.warning("tray: pystray and Pillow are not both installed")
            return False
        try:
            import pystray

            menu = pystray.Menu(
                pystray.MenuItem("Oynani ochish", lambda: self._on_show(), default=True),
                pystray.MenuItem("To'xtatish (STOP)", lambda: self.stop_work()),
                pystray.MenuItem(self._panic_label, lambda: self.panic()),
                pystray.MenuItem("Chiqish", lambda: self.quit()),
            )
            self._icon = pystray.Icon("coworker", self._picture(), self._title, menu)
            # run_detached keeps Tk's mainloop as the process's main loop.
            self._icon.run_detached()
            return True
        except Exception as exc:
            log.warning("tray unavailable: %s", exc)
            self._icon = None
            return False

    def stop(self) -> None:
        """Remove the icon from the notification area. The process keeps running."""
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

    def quit(self) -> None:
        """Stop the assistant, remove the icon and end the process.

        runtime.stop() runs on a helper thread with a bounded wait, so a stuck
        runtime cannot keep the process alive after the user chose Quit.
        """
        self.stop()
        worker = threading.Thread(target=self._stop_runtime, name="runtime-stop", daemon=True)
        worker.start()
        worker.join(STOP_TIMEOUT_S)
        hard_exit(0)

    def notify(self, message: str) -> None:
        if self._icon is None:
            return
        try:
            self._icon.notify(message[:200], "Coworker")
        except Exception:
            pass  # not every platform backend supports balloons

    # ---------------------------------------------------------------- state

    def update(self, state: str, tooltip: str) -> None:
        """Record the connection state and redraw the icon if anything visible changed."""
        self._state = state
        self._tooltip = tooltip
        self.refresh()

    def refresh(self) -> None:
        """Redraw the icon when the state or the panic flag changed. Safe to call often."""
        if self._icon is None:
            return
        panic = self._panic()
        shown = (self._state, panic, self._tooltip)
        if shown == self._shown:
            return
        self._shown = shown
        try:
            self._icon.icon = self._picture(panic)
            prefix = "PANIC — " if panic else ""
            self._icon.title = f"{self._title} — {prefix}{self._tooltip}"[:127]
        except Exception as exc:
            log.warning("tray icon update failed: %s", exc)

    # -------------------------------------------------------------- actions

    def stop_work(self) -> None:
        try:
            self._runtime.kill.stop()
        except Exception as exc:
            log.error("STOP from the tray failed: %s", exc)
        self.refresh()

    def panic(self) -> None:
        if not _confirm(PANIC_TITLE, PANIC_QUESTION):
            return
        try:
            self._runtime.kill.panic()
        except Exception as exc:
            log.error("PANIC from the tray failed: %s", exc)
        self.refresh()

    # -------------------------------------------------------------- helpers

    def _stop_runtime(self) -> None:
        try:
            self._runtime.stop()
        except Exception as exc:
            log.error("runtime stop failed: %s", exc)

    def _panic(self) -> bool:
        try:
            return bool(self._runtime.kill.is_panic())
        except Exception as exc:
            log.warning("tray could not read the panic state: %s", exc)
            return False

    def _panic_label(self, _item: Any) -> str:
        return "PANIC — yoqilgan" if self._panic() else "PANIC"

    def _picture(self, panic: bool = False):
        colour = self.PANIC_COLOUR if panic else self.COLOURS.get(self._state, "#8a94a6")
        return _icon_image(colour, panic)
