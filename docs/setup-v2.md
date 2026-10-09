# Coworker v2: setup and first run

Coworker v2 runs on your laptop and talks to you through a Telegram bot. The
laptop connects out to Telegram itself, so no server is needed. The old Render
relay in `server/` is no longer used.

## 1. What you need

- Windows 11, Python 3.13.
- A Telegram bot token. Use the one from BotFather that the old Render service
  used, or create a new bot. Keep it private.
- An LLM API key for one provider (DeepSeek, Groq, OpenRouter, GLM, Anthropic,
  or a local Ollama that needs no key).

## 2. Install

From the repository root:

```bash
pip install -r agent/requirements.txt
playwright install chromium
```

Optional: `pip install faster-whisper av` for voice messages (already in the
requirements file, uncomment vosk there if you want the lighter engine).

## 3. Check the machine

```bash
cd agent
python run.py --doctor
```

The doctor prints a checklist and exits with 0 when everything required is in
place. It never prints secret values. A fresh machine will report the Telegram
token and the LLM key as missing; that is expected before step 4.

## 4. Start the app and enter the settings

```bash
cd agent
python run.py
```

Open **Settings** and fill in:

| Field | Stored in |
|---|---|
| Telegram bot token | Windows Credential Manager (`telegram_bot_token`) |
| LLM preset and model | `config.json` |
| LLM API key | Windows Credential Manager (`llm_api_key`) |
| Speech-to-text on/off and engine | `config.json` |
| Start with Windows | the per-user Run key |

Secrets never go into `config.json`. If you prefer environment variables, set
`COWORKER_TELEGRAM_BOT_TOKEN` and `COWORKER_LLM_API_KEY` instead.

Some settings are read only when the assistant starts (the token, the key, the
model, the speech engine). The window tells you when a restart is needed.

## 5. Pair your Telegram account

The window shows a pairing code with the command `/connect <8 digits>`. The
code is valid for ten minutes and works once.

Open your bot in Telegram, in a private chat, and send that command. The first
successful pairing pins your Telegram user id and this chat. Nobody else can
talk to the assistant from then on, even if they learn the code.

If you make five wrong guesses in a row, or twenty in an hour, pairing locks
for an hour. Use **Yangi kod** in the window to clear the lock and get a new
code; that works only from the desktop.

## 6. Autonomy and the emergency buttons

The window has three autonomy levels. The default is **ask for writes**.

| Level | What runs without asking |
|---|---|
| ask always | Reads only. Everything else needs your tap. |
| ask for writes (default) | Reads, notes and reminders, and messages to your own chat. Writes, deletes, sending to others and system changes need your tap. |
| autonomous read-only | Reads and notes only. Used for scheduled jobs. Nothing else runs unattended. |

Buttons:

- **STOP** cancels what is running now. It does not change the autonomy level.
- **PANIC** stops everything, including reads, until you resume it from the
  desktop. Telegram has no resume command, so a stolen phone cannot resume it.
- **Mahalliy tasdiq** (local approval) appears for actions that need your
  approval on the laptop as well as on the phone, such as typing into a
  terminal or an IDE.

## 7. Things that are off until you turn them on

- **Deleting files** is disabled. It sends files to the Recycle Bin, and it
  stays off until the Recycle Bin behaviour has been checked on your machine
  (OneDrive placeholders, external drives, very large files). When that is done,
  set `"recycle_verified": true` in `config.json`.
- **Free-text shell commands** always need your tap, and they show the exact
  command and folder first. Commands that permanently delete, format disks,
  change the boot configuration or disable security are refused outright.
- **Mail and calendar** are not connected yet. They need provider credentials
  that you have not supplied.

## 8. What the assistant will and will not do

It will search and read your files, open and read spreadsheets and PDFs, drive
apps and the browser, run read-only system commands, keep notes and reminders,
and send you files it found.

It will not: enter passwords, card numbers or other payment details; make
payments or transfers; permanently delete anything; run as administrator; turn
off security software or the firewall. These are refused in code, not by
asking the model politely.

## 9. Where things live

- Settings: `%APPDATA%\Coworker\config.json`
- Database (notes, reminders, approvals, audit log, pairing):
  `%APPDATA%\Coworker\coworker.db`
- File index: `%APPDATA%\Coworker\fileindex.db`
- Generated files (PDFs, screenshots): `%APPDATA%\Coworker\scratch`
- Secrets: Windows Credential Manager, service name `Coworker`.

Back up the `%APPDATA%\Coworker` folder if you want to keep your notes and the
audit history.
