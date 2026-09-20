"""Entry point for the desktop agent.

    python run.py              # control panel window
    python run.py --headless   # no GUI, useful on a server or for debugging
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from coworker.config import Config


def _headless(cfg: Config) -> None:
    from coworker.app import CoworkerAgent

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    print(f"\n  Coworker · {cfg.get('name')}")
    print(f"  Telegramda yuboring:  /connect {cfg.pair_code}\n")

    agent = CoworkerAgent(cfg, lambda state, detail: print(f"  [{state}] {detail}"))
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("\n  To'xtatildi.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="coworker")
    parser.add_argument("--headless", action="store_true", help="GUI'siz ishga tushirish")
    parser.add_argument("--config", type=Path, help="Boshqa config fayli")
    args = parser.parse_args()

    cfg = Config(args.config)
    try:
        if args.headless:
            _headless(cfg)
        else:
            from coworker.ui import AgentWindow
            AgentWindow(cfg).start()
    except KeyboardInterrupt:
        # Ctrl+C in the launching terminal is a normal way to stop this,
        # not a crash - a traceback here just looks like a bug.
        print("\n  Coworker to'xtatildi.")


if __name__ == "__main__":
    main()
