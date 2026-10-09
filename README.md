# Coworker

A personal assistant that runs on your Windows laptop and works for you
through a Telegram bot. You write (or send a voice note) in plain language;
the laptop does the work and answers in the chat.

It can search and read your files, open and read spreadsheets and PDFs,
drive apps and the browser, keep notes and reminders, run scheduled jobs,
and send you files it finds. Everything that changes something on the
machine, or sends something out, waits for your tap in Telegram.

> The previous version used a Render relay and was built for a relative to
> find documents. That relay is still in `server/`, but nothing uses it now.
> The old README is in the git history.

## Quick start

Full steps are in [docs/setup-v2.md](docs/setup-v2.md). In short:

```bash
pip install -r agent/requirements.txt
playwright install chromium
cd agent
python run.py --doctor     # checks the machine
python run.py              # opens the control window
```

Enter your Telegram bot token and LLM key in Settings. They are stored in
Windows Credential Manager, not in a file. Then send the `/connect` code the
window shows to your bot in a private chat.

## How it is built

```
  Telegram  ──long poll──▶  Coworker (your laptop)
  (owner only)               ├─ owner gate: one pinned Telegram user and chat
                             ├─ orchestrator: bounded model conversation
                             ├─ dispatcher: the only path to a side effect
                             │    └─ policy kernel: allow / confirm / deny
                             ├─ approvals: frozen actions, one tap each
                             ├─ tools: files, office, desktop, apps, system,
                             │         browser, web, shell, notes, schedule
                             ├─ governor: pools, pressure pauses, timeouts
                             └─ store: SQLite (notes, approvals, audit log)
```

Policy is code. The model is told the rules, but a refused action is refused
by the kernel whatever the model says. The full contract is in
[docs/architecture-v2.md](docs/architecture-v2.md).

## Safety model

- **Owner only.** Pairing pins one Telegram user id and one private chat.
  Messages from anyone else are dropped before they are read.
- **Autonomy.** Ask always, ask for writes (default), or autonomous read-only
  for scheduled jobs. PANIC stops everything; only the desktop can resume it.
- **Confirmation.** Writes, sends and system changes show a card with the
  exact arguments. A tap runs that action once; a replayed tap does nothing.
- **Untrusted content.** Text from documents, pages, screens, file names and
  window titles cannot drive a risky action on its own. Values that appear
  only in such content are refused.
- **Never permanent.** File deletes go to the Recycle Bin, and stay off until
  you enable them. Shell commands that permanently delete, format disks, or
  change boot or security settings are refused.
- **Never money or passwords.** Payment and credential actions are refused in
  code, and card numbers are caught in typed text, including split across
  calls.
- **Limits.** Heavy work runs in pools with pauses when the machine is busy,
  and there are daily budgets. Every action is written to a hash-chained
  audit log.

## Layout

```
agent/
  run.py                 control window, --headless, --doctor
  coworker/
    runtime.py           wires everything together
    policy/              kernel, matrix, lexicon, paths, shell rules
    tools/               one module per tool family; each exports SPECS
    orchestrator/        turn loop and system prompt
    safety/              approvals, kill switch, pairing
    store/               SQLite, secrets, legacy import
    transport/           Telegram client, owner gate, polling, outbox
    governor/            pools, pressure signals, budgets
    fileindex/           search index over your folders
    llm_base.py          provider protocol; llm.py and llm_anthropic.py adapt
  tests/                 pytest; fakes at the Telegram, model and OS edges
docs/
  setup-v2.md            install and first run
  architecture-v2.md     the contract, and the changes after review
server/                  legacy Render relay (unused)
```

## Tests

```bash
cd agent
python -m pytest tests
```

The suite uses fakes at the Telegram, model and operating-system edges. The
end-to-end tests run the real runtime through pairing, a tool call, an
approval, a replayed tap and a panic.

## Status

Tested with fakes only. It has not yet been run against a live bot, a live
model, or a real desktop session. Known gaps, mail and calendar, and voice
replies are listed in section 15 of [docs/architecture-v2.md](docs/architecture-v2.md).

## License

MIT
