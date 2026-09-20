"""System tray presence and Windows autostart.

The agent is only useful while it is running, but nobody keeps a window open
all day - they hit the X. Closing the window therefore hides it to the tray
instead of quitting, and the agent keeps serving Telegram from there.

Autostart is deliberately the per-user Run key rather than a Windows service:
a service runs in session 0 with no desktop, which would break the moment this
agent is ever asked to interact with the screen.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("tray")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Coworker"


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
            return ""
        script = fallback

    # pythonw.exe keeps a console window from flashing up on every login.
    exe = Path(sys.executable)
    quiet = exe.with_name("pythonw.exe")
    runner = quiet if quiet.exists() else exe
    return f'"{runner}" "{script}"'


def autostart_enabled() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_NAME)
            return bool(value)
    except (FileNotFoundError, OSError):
        return False


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
                return True, "Kompyuter yonganda avtomatik ishga tushadi"
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass
            return True, "Avtomatik ishga tushish o'chirildi"
    except OSError as exc:
        log.warning("autostart failed: %s", exc)
        return False, f"Xato: {exc}"


# ----------------------------------------------------------------- tray icon

def available() -> bool:
    import importlib.util

    return bool(
        importlib.util.find_spec("pystray") and importlib.util.find_spec("PIL")
    )


def _icon_image(colour: str):
    """Draw the icon in memory so the build needs no asset file."""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # A document sheet with a folded corner, tinted by connection state.
    d.rounded_rectangle([12, 6, 52, 58], radius=6, fill=colour)
    d.polygon([(38, 6), (52, 20), (38, 20)], fill="#0b0d12")
    for y in (30, 38, 46):
        d.line([20, y, 44, y], fill="#0b0d12", width=3)
    return img


class Tray:
    """Thin wrapper so the UI does not need to know pystray exists."""

    COLOURS = {"online": "#3ddc84", "working": "#4a9eff", "offline": "#ff5c5c"}

    def __init__(self, on_show, on_quit, title: str = "Coworker") -> None:
        self._icon = None
        self._on_show = on_show
        self._on_quit = on_quit
        self._title = title

    def start(self) -> bool:
        if not available():
            return False
        try:
            import pystray

            menu = pystray.Menu(
                pystray.MenuItem("Oynani ochish", lambda: self._on_show(), default=True),
                pystray.MenuItem("Chiqish", lambda: self._quit()),
            )
            self._icon = pystray.Icon(
                "coworker", _icon_image(self.COLOURS["offline"]), self._title, menu
            )
            # run_detached keeps Tk's mainloop as the process's main loop.
            self._icon.run_detached()
            return True
        except Exception as exc:
            log.warning("tray unavailable: %s", exc)
            self._icon = None
            return False

    def update(self, state: str, tooltip: str) -> None:
        if self._icon is None:
            return
        try:
            self._icon.icon = _icon_image(self.COLOURS.get(state, "#8a94a6"))
            self._icon.title = f"Coworker — {tooltip}"[:127]
        except Exception:
            pass

    def notify(self, message: str) -> None:
        if self._icon is None:
            return
        try:
            self._icon.notify(message[:200], "Coworker")
        except Exception:
            pass  # not every platform backend supports balloons

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

    def _quit(self) -> None:
        self.stop()
        self._on_quit()
