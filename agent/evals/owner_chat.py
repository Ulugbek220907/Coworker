"""Replay real owner messages through the assistant with the real model.

Only the edges are fake: the Telegram client records what would be sent, app
launches and folder opens are recorded instead of run, keystrokes are recorded,
and the on-screen check is off. The model, the policy, the approvals and the
orchestrator are the real ones. Each approval card is answered "yes", as the
owner would tap it, so the run shows what the assistant tried.

    cd agent
    python evals/owner_chat.py            # all scenarios, one run each
    python evals/owner_chat.py --runs 3   # repeat, because the model varies

Writes a JSON transcript next to this file under evals/out/. Results are shown
with the checks that passed or failed; a failed check is a finding, not a crash.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import types
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT))
os.environ["COWORKER_HOME"] = tempfile.mkdtemp(prefix="coworker-eval-")  # never the owner's database

from coworker import keys, launcher, uia  # noqa: E402
from coworker.config import Config  # noqa: E402
from coworker.core.ports import PowerState  # noqa: E402
from coworker.runtime import Runtime  # noqa: E402
from coworker.tools import apps  # noqa: E402

OWNER = 42
EVAL_OUT = AGENT / "evals" / "out"


class FakeApi:
    """Records the bot's replies. Only the methods the runtime calls."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_message(self, chat_id, text, buttons=None, reply_to=None) -> dict:
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons})
        return {"ok": True, "result": {"message_id": len(self.sent)}}

    def send_document(self, *a, **k) -> dict:
        return {"ok": True}

    def send_photo(self, *a, **k) -> dict:
        return {"ok": True}

    def answer_callback(self, *a, **k) -> dict:
        return {"ok": True}

    def edit_reply_markup(self, *a, **k) -> dict:
        return {"ok": True}

    def get_file_bytes(self, *a, **k) -> dict:
        return {"ok": False}

    def delete_webhook(self) -> dict:
        return {"ok": True}

    def close(self) -> None:
        pass


class CalmOs:
    def idle_seconds(self) -> float:
        return 600.0

    def cpu_percent(self) -> float:
        return 5.0

    def free_ram_mb(self) -> float:
        return 8192.0

    def power(self) -> PowerState:
        return PowerState(percent=None, plugged=None, saver=False)

    def is_elevated(self) -> bool:
        return False

    def input_desktop_available(self) -> bool:
        return True


class World:
    """What the assistant did to the machine, as recorded (nothing was really run)."""

    def __init__(self) -> None:
        self.launched: list[str] = []
        self.folder_opens: list[list[str]] = []
        self.typed: list[str] = []
        self.clipboard: list[str] = []
        self.keys_pressed: list[str] = []

    def install(self) -> None:
        def fake_start(path: str, name: str) -> dict:
            self.launched.append(name)
            return {"ok": True, "opened": name}

        launcher._start = fake_start
        apps.subprocess = types.SimpleNamespace(
            Popen=lambda args, **kw: self.folder_opens.append(list(args)) or None,
        )
        # The window list is stubbed too: an app opened by the stubbed launch is not running.
        uia.list_windows = lambda: {"windows": [
            {"title": "MiniAI - Antigravity", "handle": 7, "class": "", "pid": 1}], "count": 1}
        uia.find_window = lambda title: (
            {"handle": 7, "title": "Antigravity"} if "antigravity" in title.lower()
            else {"error": f"«{title}» oynasi topilmadi", "code": "arg_invalid"}
        )
        keys.type_text = lambda text, handle, blocked=(): (self.typed.append(text), {"ok": True})[1]
        keys.press = lambda combo, handle, blocked=(): (self.keys_pressed.append(combo), {"ok": True})[1]
        keys.clipboard_set = lambda text: (self.clipboard.append(text), {"ok": True, "chars": len(text)})[1]


def _message(text: str, message_id: int) -> dict:
    return {"message_id": message_id, "chat": {"id": OWNER, "type": "private"},
            "from": {"id": OWNER}, "text": text}


def _tap(data: str, message_id: int) -> dict:
    return {"id": f"cb{message_id}", "from": {"id": OWNER},
            "message": {"message_id": message_id, "chat": {"id": OWNER, "type": "private"}}, "data": data}


async def play(rt: Runtime, api: FakeApi, text: str, max_taps: int = 4) -> list[dict]:
    """Send one owner message, then answer each approval card with a yes, as a tap would."""
    calls: list[dict] = []
    original = rt.dispatcher.invoke

    async def recording(name, args, turn):
        result = await original(name, args, turn)
        calls.append({"tool": name, "args": args, "ok": result.ok, "code": result.code,
                      "message": (result.data or {}).get("message") or result.error})
        return result

    rt.dispatcher.invoke = recording
    mid = len(api.sent) + 1
    await rt._on_message(_message(text, mid))
    for tap in range(max_taps):
        last = api.sent[-1] if api.sent else None
        if not last or not last.get("buttons"):
            break
        yes = next((b["callback_data"] for row in last["buttons"] for b in row
                    if b["callback_data"].endswith(":y")), None)
        if yes is None:
            break
        await rt._on_callback(_tap(yes, len(api.sent) + 1))
        calls.append({"tool": "<owner tapped yes>", "args": {}, "ok": True, "code": "", "message": ""})
    rt.dispatcher.invoke = original
    return calls


def _owner_texts(api: FakeApi) -> list[str]:
    return [m["text"] for m in api.sent if m["chat_id"] == OWNER]


def _check(label: str, passed: bool, detail: str = "") -> dict:
    return {"check": label, "passed": bool(passed), "detail": detail}


SCENARIOS = [
    {
        "name": "open antigravity",
        "says": ["Open antigravity"],
        "checks": lambda w, calls, texts: [
            _check("opens Antigravity", any("antigravity" in n.lower() for n in w.launched), str(w.launched)),
            _check("asks no pointless question", not any(t.rstrip().endswith("?") for t in texts[-1:]), texts[-1:] and texts[-1][:80]),
        ],
    },
    {
        "name": "open the project and write into it",
        # The real chat: Antigravity was opened in the previous message, so the project is in it.
        "says": ["Open antigravity", "Open mini ai project and write: finish this project"],
        "checks": lambda w, calls, texts: [
            _check("looks for the project folder first",
                   any(c["tool"] == "find_folder" for c in calls), str([c["tool"] for c in calls])),
            _check("writes the text with the typing tool, not the clipboard",
                   "finish this project" in w.typed and not any(c["tool"] == "clipboard_set" for c in calls),
                   f"typed={w.typed} clipboard={w.clipboard}"),
            _check("does not press Enter while only writing",
                   "enter" not in w.keys_pressed, str(w.keys_pressed)),
            _check("opens the project folder only after finding it",
                   (not w.folder_opens) or any(c["tool"] == "find_folder" for c in calls),
                   str(w.folder_opens)),
            _check("opens the project inside Antigravity",
                   any("antigravity" in " ".join(p).lower() for p in w.folder_opens), str(w.folder_opens)),
            _check("does not claim the text is verified on screen",
                   not any("ekranda tekshirildi" in t for t in texts), texts[-1][:120] if texts else ""),
        ],
    },
    {
        "name": "a website name without a URL",
        "says": ["Eclassga kit"],
        "checks": lambda w, calls, texts: [
            _check("does not open a made-up address",
                   not any(c["tool"] == "web_open" and "eclass" in str(c["args"]).lower() for c in calls),
                   str([c for c in calls if c["tool"] == "web_open"])),
            _check("asks for the address or a choice",
                   bool(texts) and any(k in texts[-1].lower() for k in
                                       ("?", "manzil", "url", "aytsangiz", "yuboring", "qaysi")),
                   texts[-1][:120] if texts else "no reply"),
        ],
    },
    {
        "name": "browser without a preference asks",
        "says": ["Open browser"],
        "checks": lambda w, calls, texts: [
            _check("does not guess a browser", not w.launched, str(w.launched)),
            _check("asks which browser, with Chrome and Edge as the choices",
                   any(c["tool"] == "ask" and any("chrome" in o.lower() for o in c["args"].get("options", []))
                       and any("edge" in o.lower() for o in c["args"].get("options", []))
                       for c in calls),
                   str([c["args"].get("options") for c in calls if c["tool"] == "ask"])),
            _check("uses the lookup tools before asking",
                   any(c["tool"] in ("default_browser", "installed_browsers") for c in calls),
                   str([c["tool"] for c in calls])),
        ],
    },
    {
        "name": "a stated preference is remembered and used",
        "says": ["Brauzer deganda men Google Chrome ni nazarda tutaman", "Open browser"],
        "checks": lambda w, calls, texts: [
            _check("opens Chrome, the owner's stated browser",
                   any("chrome" in n.lower() for n in w.launched), str(w.launched)),
            _check("does not open Edge", not any("edge" in n.lower() for n in w.launched), str(w.launched)),
        ],
    },
]


async def run_once(selected: list[str] | None, run_no: int = 1) -> list[dict]:
    results = []
    for scenario in SCENARIOS:
        if selected and scenario["name"] not in selected:
            continue
        world = World()
        world.install()
        cfg = Config(Path(os.environ["COWORKER_HOME"]) / f"config-{scenario['name'][:12].replace(' ', '_')}.json")
        api = FakeApi()
        # One database per scenario and run: a scenario must not inherit another's history or facts.
        db = Path(os.environ["COWORKER_HOME"]) / f"eval-{run_no}-{scenario['name'][:24].replace(' ', '_')}.db"
        rt = Runtime(cfg, api=api, os_port=CalmOs(), store_path=db)
        rt._loop = asyncio.get_running_loop()
        code = rt.pairing.issue_code()
        rt.pairing.redeem(code, from_id=OWNER, chat_id=OWNER, chat_type="private")
        calls: list[dict] = []
        for line in scenario["says"]:
            calls.extend(await play(rt, api, line))
        texts = _owner_texts(api)
        checks = scenario["checks"](world, calls, texts)
        results.append({
            "scenario": scenario["name"],
            "says": scenario["says"],
            "tool_calls": calls,
            "owner_saw": texts,
            "world": {"launched": world.launched, "folder_opens": world.folder_opens,
                      "typed": world.typed, "clipboard": world.clipboard, "keys": world.keys_pressed},
            "checks": checks,
        })
        rt.kill.stop()
        rt.services.scheduler.stop()
    return results


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--only", action="append", default=None, help="scenario name to run (repeatable)")
    args = parser.parse_args()
    EVAL_OUT.mkdir(parents=True, exist_ok=True)
    all_runs = []
    out = EVAL_OUT / "transcript.json"
    for run in range(args.runs):
        results = asyncio.run(run_once(args.only, run + 1))
        all_runs.append({"run": run + 1, "results": results})
        out.write_text(json.dumps(all_runs, ensure_ascii=False, indent=1), encoding="utf-8")
        for r in results:
            passed = sum(c["passed"] for c in r["checks"])
            print(f"[run {run + 1}] {r['scenario']}: {passed}/{len(r['checks'])} checks")
            for c in r["checks"]:
                print(f"   {'PASS' if c['passed'] else 'FAIL'}  {c['check']}  ({c['detail'][:100]})")
    out = EVAL_OUT / "transcript.json"
    out.write_text(json.dumps(all_runs, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\ntranscript: {out}")


if __name__ == "__main__":
    main()
