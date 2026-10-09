"""Entry point for the desktop agent.

    python run.py              # control window with a tray icon
    python run.py --headless   # no window: prints the pairing code and runs until Ctrl+C
    python run.py --doctor     # checks what this PC needs; exits 0 when every required item is present
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from coworker.config import Config, config_dir  # noqa: E402
from coworker.store.secrets import get_secret  # noqa: E402
from coworker.tray import hard_exit  # noqa: E402

TOKEN_SECRET = "telegram_bot_token"   # the name the runtime stores the token under
MIN_PYTHON = (3, 11)


# ------------------------------------------------------------------ doctor

class Check(NamedTuple):
    name: str
    ok: bool
    required: bool
    detail: str


def check_python() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    ok = sys.version_info[:2] >= MIN_PYTHON
    return Check("Python", ok, True, f"{version} (kerak: {MIN_PYTHON[0]}.{MIN_PYTHON[1]} yoki yangi)")


def check_keyring() -> Check:
    name = "Kalit saqlagich (keyring)"
    try:
        import keyring
    except ImportError:
        return Check(name, False, True, "o'rnatilmagan: pip install keyring")
    try:
        backend = keyring.get_keyring()
    except Exception:
        return Check(name, False, True, "backend topilmadi")
    label = f"{type(backend).__module__}.{type(backend).__name__}"
    ok = not any(tag in label.lower() for tag in ("fail", "null"))
    return Check(name, ok, True, label if ok else f"mavjud emas ({label})")


def check_psutil() -> Check:
    name = "Tizim yuki (psutil)"
    try:
        import psutil
    except ImportError:
        return Check(name, False, True, "o'rnatilmagan: pip install psutil")
    return Check(name, True, True, f"versiya {psutil.__version__}")


def check_telegram_token() -> Check:
    # Presence only. The value is never read into the output.
    present = bool(get_secret(TOKEN_SECRET))
    detail = "saqlangan" if present else "kiritilmagan (Sozlamalar bo'limida kiriting)"
    return Check("Telegram tokeni", present, True, detail)


def check_llm(cfg: Config) -> Check:
    from coworker.llm_base import ProviderError, build_provider

    name = "AI modeli"
    try:
        provider = build_provider(cfg, get_secret)
    except ProviderError as exc:
        return Check(name, False, True, str(exc))
    has_key = bool(cfg.get("llm_api_key"))
    detail = f"{getattr(provider, 'dialect', '?')}, model {cfg.get('llm_model')}"
    if not has_key:
        detail += ", API kaliti yo'q"
    return Check(name, has_key, True, detail)


def check_home() -> Check:
    home = config_dir()  # creates the folder when it is missing
    source = "COWORKER_HOME" if os.getenv("COWORKER_HOME", "").strip() else "standart joy"
    writable = os.access(home, os.W_OK)
    detail = f"{home} ({source})" + ("" if writable else ", yozib bo'lmaydi")
    return Check("COWORKER_HOME", writable, True, detail)


def check_index_path() -> Check:
    path = config_dir() / "fileindex.db"
    state = "mavjud" if path.exists() else "birinchi ishga tushirishda yaratiladi"
    return Check("Fayl indeksi (baza)", True, False, f"{path}, {state}")


def check_elevated() -> Check:
    elevated = _is_elevated()
    detail = ("ha: oddiy foydalanuvchi sifatida ishlatish tavsiya etiladi" if elevated else "yo'q")
    return Check("Administrator huquqi", not elevated, False, detail)


def _is_elevated() -> bool:
    if os.name == "nt":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def _mark(item: Check) -> str:
    if item.ok:
        return "OK"
    return "YO'Q" if item.required else "eslatma"


def run_doctor(cfg: Config) -> int:
    """Print the checklist. Returns 0 when every required item is present, else 1."""
    checks = [
        check_python(), check_keyring(), check_psutil(), check_telegram_token(),
        check_llm(cfg), check_home(), check_index_path(), check_elevated(),
    ]
    print("\n  Coworker tekshiruvi\n")
    for item in checks:
        print(f"  {_mark(item):<9}{item.name:<26}{item.detail}")
    missing = [item.name for item in checks if item.required and not item.ok]
    print()
    if missing:
        print("  Yetishmayapti: " + ", ".join(missing))
        return 1
    print("  Hammasi tayyor.")
    return 0


# ----------------------------------------------------------------- modes

def console_status(box: dict[str, Any]) -> Callable[[str, str], None]:
    """Status callback for headless mode.

    On "online" the runtime has just issued a pairing code of its own, and the code
    in its detail is stale once this issues another. The code printed here is the
    one that works.
    """
    def status(state: str, detail: str) -> None:
        runtime = box.get("runtime")
        if state == "online" and runtime is not None:
            code = runtime.pairing.issue_code()
            print(f"  [online] Telegramda yuboring:  /connect {code}", flush=True)
            return
        print(f"  [{state}] {detail}", flush=True)

    return status


class RedactingFormatter(logging.Formatter):
    """Formats a record and removes secrets from it, the same way the window's journal does."""

    def format(self, record: logging.LogRecord) -> str:
        from coworker.ui_panels import redact_line

        return redact_line(super().format(record))


def run_headless(cfg: Config) -> int:
    from coworker.runtime import Runtime  # deferred: the runtime pulls in the whole assistant
    from coworker.transport.bot import install_redaction
    from coworker.ui_panels import LOG_FORMAT

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, "%H:%M:%S"))
    logging.getLogger().handlers[:] = [handler]
    logging.getLogger().setLevel(logging.INFO)
    install_redaction()

    box: dict[str, Any] = {}
    runtime = Runtime(cfg, status=console_status(box))
    box["runtime"] = runtime
    print(f"\n  Coworker · {cfg.get('name')}", flush=True)
    try:
        asyncio.run(runtime.run())
    except KeyboardInterrupt:
        print("\n  To'xtatildi.")
    finally:
        runtime.stop()
    return 0


def run_gui(cfg: Config) -> int:
    from coworker import ui

    if not ui.claim_single_instance():
        ui.notify_already_running()
        return 0
    try:
        window = ui.AgentWindow(cfg)
    except Exception as exc:
        logging.getLogger("run").exception("the window could not start")
        ui.show_fatal(f"Coworker ishga tushmadi: {exc}")
        return 1
    window.start()
    return 0


# --------------------------------------------------------------- entry

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coworker", description="Coworker kompyuter yordamchisi")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--headless", action="store_true",
                      help="oynasiz ishga tushirish: ulash kodini chiqaradi")
    mode.add_argument("--doctor", action="store_true",
                      help="kompyuter tayyorligini tekshirish")
    parser.add_argument("--config", type=Path, help="boshqa config fayli")
    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the chosen mode and return the exit code. The process itself is ended by run()."""
    args = parse_args(argv)
    cfg = Config(args.config)
    if args.doctor:
        return run_doctor(cfg)
    if args.headless:
        return run_headless(cfg)
    return run_gui(cfg)


def _prepare_console() -> None:
    """Printing Uzbek text to a legacy console code page must not crash the run."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


def run() -> None:
    """Script entry point. The process always ends here, with the code main() chose."""
    code = 1
    try:
        _prepare_console()
        code = main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except KeyboardInterrupt:
        print("\n  Coworker to'xtatildi.")
        code = 0
    except Exception:
        if sys.stderr is not None:
            traceback.print_exc()
    finally:
        # Backstop. Returning normally is not enough to end the process: tray and COM
        # worker threads can hold it open, and once the interpreter has begun shutting
        # down, the default executor is dead, so a surviving agent answers every Telegram
        # message with an internal error instead of going quiet. hard_exit flushes the
        # logs and ends the process whatever happened above.
        hard_exit(code)


if __name__ == "__main__":
    run()
