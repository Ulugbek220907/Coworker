# Coworker v2 — architecture and build contracts

This document is the contract every module is built against. Where code and
this file disagree, the code is wrong until this file is changed on purpose.

## 1. Principles

1. **Policy is code.** Every side effect passes through `policy.kernel.PolicyKernel`.
   Prompts describe the tools to the model; they never stop an action.
2. **One path for effects.** Tools never run themselves. The dispatcher validates
   arguments, asks the kernel, budgets, audits and only then calls the governor.
3. **Owner-pinned.** Only the owner's private Telegram chat is trusted. Identity is
   a numeric user id pinned at first pairing, not a shared code.
4. **Content is data.** Text read from documents, pages, screens, mail or clipboard
   is `CONTENT`. Once a turn has seen content, sensitive values that appear only in
   that content are refused (origin check), and risky actions need a tap.
5. **Nothing is permanently deleted.** Deletion goes to the Recycle Bin, and it is
   switched off until the Recycle Bin path is verified on the laptop.
6. **The laptop stays usable.** Heavy work runs under a governor with pools,
   priorities, pressure pauses, timeouts and a kill switch.
7. **Reuse the OS modules.** `uia.py`, `keys.py`, `browser.py`, `vision.py`, `fs.py`,
   `extract.py`, `textutil.py`, `office.py`, `system.py`, `launcher.py`, `stt.py`,
   `tray.py` and `ui.py` stay flat in `agent/coworker/`. Tools wrap them.

## 2. Layout

```
agent/coworker/
  core/        types.py (Tier, Decision, Autonomy, Provenance, Verdict, CallContext, ToolResult)
               ports.py (OsPort protocol)                                    [done]
  policy/      kernel.py, matrix.py, lexicon.py, origin.py                   [done]
               paths.py, prohibited.py, urls.py, shell_rules.py              [wave 1]
  tools/       registry.py (ToolSpec, Registry, validate_args, load_all)     [done]
               dispatch.py, turn.py (orchestrator loop)                      [wave 2]
               files.py, office.py, desktop.py, apps.py, system.py,
               browser.py, web.py, shell.py, notes.py, schedule.py,
               telegram_out.py                                               [wave 1]
  store/       db.py (Store), migrations, secrets.py                         [wave 1]
  safety/      approvals.py, killswitch.py, pairing.py                       [wave 1]
  governor/    governor.py, signals.py, budget.py                            [wave 1]
  fileindex/   index.py, crawl.py                                            [wave 1]
  transport/   bot.py (BotApi), outbox.py, poll.py, gate.py, commands.py     [wave 1]
  llm_base.py, llm.py (OpenAI-compatible, presets), llm_anthropic.py        [wave 1]
  scheduler.py                                                               [wave 1]
  runtime.py   composition root                                              [wave 2]
  (flat, wrapped)  uia.py keys.py browser.py vision.py fs.py extract.py
                   textutil.py office.py system.py launcher.py stt.py tray.py ui.py
  (legacy, retired) brain.py, memory.py, link.py  — removed at the end of wave 2
agent/tests/   pytest; no network, no real Telegram, no real LLM, no GUI.
```

## 3. Tool contract

Every tool module defines a module-level `SPECS: list[ToolSpec]`. Nothing registers
itself on import. `tools.registry.load_all()` imports the modules listed in
`TOOL_MODULES` and skips missing ones.

`ToolSpec` fields (see `tools/registry.py`):

| field | meaning |
|---|---|
| `name` | `^[a-z][a-z0-9_]{1,40}$`, unique |
| `family` | one of `FAMILIES`; the owner grants families in config |
| `tier` | `Tier`; FINANCIAL and CREDENTIAL are refused at registration |
| `description`, `parameters` | sent to the model; `parameters` is a JSON-Schema object |
| `handler(ToolCall) -> ToolResult` | sync; runs in a governor thread |
| `gov_class` | one of `NONE, TOOL, UIA, INPUT, VISION, BROWSER, OFFICE, SHELL, INDEX, STT, NET` |
| `timeout_s` | hard limit for the handler |
| `untrusted` | output is content; the orchestrator marks the turn CONTENT |
| `internal` | writes only Coworker's own store (LOCAL_WRITE only) |
| `self_target` | OUTBOUND only to the owner's own chat (OUTBOUND only) |
| `path_args`, `path_write` | arguments that are filesystem paths; written or read |
| `sensitive_args` | origin-checked for OUTBOUND, DESTRUCTIVE, SYSTEM_CHANGE |
| `requires_surfaced` | arguments that must come from this turn's searches (or delivered before) |
| `relax(args, ctx) -> bool` | owner turns only: turn CONFIRM into ALLOW for benign cases |
| `arg_checks` | `(args, ctx, svc) -> Verdict | None` hooks; may escalate |
| `summary(args) -> str` | owner-facing text on the CONFIRM card |
| `probe() -> bool` | False hides the tool (for example, no credentials) |

`ToolCall` carries `name`, `args`, `ctx` (a `CallContext`) and `svc` (`Services`).
Handlers must not reach into other tools' state.

`Services` attributes: `store`, `governor`, `budget`, `index`, `outbox`, `llm`,
`scheduler`, `kill`, `os`, `config`. Contracts for each are in sections 5–10.

### Tool catalogue (authoritative)

| tool | family | tier | gov | notes |
|---|---|---|---|---|
| find_files(query, root?, limit?) | files | READ | INDEX | index first, bounded live fallback; surfaces paths |
| list_dir(path) | files | READ | TOOL | path_args; surfaces paths |
| preview_file(path, chars?) | files | READ | TOOL | untrusted; protected paths refused |
| search_in_files(query, paths[]) | files | READ | INDEX | untrusted; each path checked |
| recent_files(days?) | files | READ | INDEX | surfaces paths |
| file_copy(src, dst) | files | LOCAL_WRITE | TOOL | path_args; requires_surfaced src |
| file_move(src, dst) | files | LOCAL_WRITE | TOOL | path_args; requires_surfaced src |
| file_rename(path, new_name) | files | LOCAL_WRITE | TOOL | path_args; requires_surfaced path |
| file_mkdir(path) | files | LOCAL_WRITE | TOOL | path_args |
| file_delete(path) | files | DESTRUCTIVE | TOOL | Recycle Bin only; probe False until `recycle_verified` config |
| sheet_list(path) | office | READ | OFFICE | path_args |
| sheet_read(path, sheet?) | office | READ | OFFICE | untrusted |
| sheet_write(path, changes, sheet?) | office | LOCAL_WRITE | OFFICE | requires_surfaced; backup first; .xlsm refused |
| to_pdf(path) | office | LOCAL_WRITE | OFFICE | writes into scratch dir only; sendable result |
| pdf_pages(path, pages) | office | LOCAL_WRITE | OFFICE | writes into scratch dir only; sendable result |
| list_windows() | desktop | READ | UIA | |
| read_window(title? / handle?) | desktop | READ | UIA | untrusted |
| screen_read(question, title?) | desktop | READ | VISION | untrusted; blocked windows never captured |
| clipboard_get() | desktop | READ | INPUT | untrusted; secret-shaped text redacted |
| ui_set_text(handle, ref, text) | desktop_control | LOCAL_WRITE | UIA | prohibited scan on text; IsPassword refused |
| key_type(handle, text) | desktop_control | LOCAL_WRITE | INPUT | handle required; focus verified in the same job |
| key_press(handle, combo) | desktop_control | LOCAL_WRITE | INPUT | relax for benign combos; Enter escalates |
| ui_click(handle, ref) | desktop_control | LOCAL_WRITE | UIA | label classified: FINANCIAL DENY, CREDENTIAL/DESTRUCTIVE/OUTBOUND/SYSTEM CONFIRM; relax for plain labels |
| clipboard_set(text) | desktop_control | LOCAL_WRITE | INPUT | prohibited scan |
| control_app_read(app) | desktop | READ | VISION | untrusted |
| control_app_send(app, text) | desktop_control | OUTBOUND | INPUT | sends into an app; two_channel for terminals and IDEs |
| open_app(name) | apps | SYSTEM_CHANGE | TOOL | relax only for indexed Start Menu shortcuts |
| window_focus(handle) | apps | READ | UIA | |
| window_state(handle, state) | apps | READ | UIA | minimize, maximize, normal |
| window_close(handle) | apps | DESTRUCTIVE | UIA | graceful close request only; reports close_requested |
| volume_get() | system | READ | TOOL | |
| volume_set(percent) | system | SYSTEM_CHANGE | TOOL | relax for owner turns; range validated |
| volume_adjust(delta) | system | SYSTEM_CHANGE | TOOL | relax for owner turns |
| volume_mute(mute) | system | SYSTEM_CHANGE | TOOL | relax for owner turns |
| system_status() | system | READ | TOOL | CPU, RAM, disk, battery, uptime |
| processes_list(limit?) | system | READ | TOOL | names and pids only; command lines dropped |
| lock_screen() | system | SYSTEM_CHANGE | TOOL | always CONFIRM |
| power_action(action) | system | SYSTEM_CHANGE | TOOL | sleep, restart, shutdown; always CONFIRM |
| web_open(url) | browser | READ | BROWSER | urls.check_url; untrusted page text later |
| web_read() | browser | READ | BROWSER | untrusted |
| web_click(ref) | browser | LOCAL_WRITE | BROWSER | fingerprint ref; label classified like ui_click |
| web_type(ref, text, submit?) | browser | LOCAL_WRITE | BROWSER | password fields refused; submit escalates to OUTBOUND-like CONFIRM |
| web_screenshot() | browser | READ | VISION | untrusted; sendable file |
| web_fetch(url, max_chars?) | web | READ | NET | urls.check_url plus DNS pinning; untrusted |
| web_search(query, limit?) | web | READ | NET | probe: search key configured; untrusted |
| shell_readonly(command_id, args?) | shell | READ | SHELL | fixed table in shell_rules; untrusted output |
| shell_run(command, cwd?) | shell | SYSTEM_CHANGE | SHELL | always CONFIRM; hard_deny checked first; two_channel; elevated refused; sensitive_args=command |
| note_add(title, body) | notes | LOCAL_WRITE | TOOL | internal |
| note_search(query) | notes | READ | TOOL | |
| note_list(limit?) | notes | READ | TOOL | |
| task_add(text, due?) | notes | LOCAL_WRITE | TOOL | internal |
| task_list(open_only?) | notes | READ | TOOL | |
| task_done(id) | notes | LOCAL_WRITE | TOOL | internal |
| remember(fact) | notes | LOCAL_WRITE | TOOL | internal; stored untrusted when provenance is CONTENT |
| recall(query) | notes | READ | TOOL | |
| forget_fact(needle) | notes | LOCAL_WRITE | TOOL | internal |
| reminder_add(text, when) | schedule | LOCAL_WRITE | TOOL | internal; fixed template delivery, no model call |
| reminder_list() | schedule | READ | TOOL | |
| reminder_cancel(id) | schedule | LOCAL_WRITE | TOOL | internal |
| job_add(name, instruction, when) | schedule | SYSTEM_CHANGE | TOOL | always CONFIRM; runs unattended at AR |
| job_list() | schedule | READ | TOOL | |
| job_cancel(id) | schedule | LOCAL_WRITE | TOOL | internal |
| send_file(path, caption?) | telegram | OUTBOUND | TOOL | self_target; requires_surfaced path; daily byte budget |
| ask(question, options) | telegram | OUTBOUND | TOOL | self_target; returns `end_turn` |
| notify(text) | telegram | OUTBOUND | TOOL | self_target |

Deferred (no tool module yet): mail (`mail.py`), calendar (`calendar.py`), speech
output (`speech.py`). They stay hidden until credentials exist.

## 4. Policy helpers (wave 1, pure functions)

- `policy/paths.py`: `check_path(value: str, *, write: bool) -> str | None`.
  Returns an error code (`path_invalid`, `protected_path`, `reparse_point`,
  `device_name`) or None. Rejects traversal after normalisation, device names
  (CON, NUL, COM1…), extended prefixes (`\\?\`, `\\.\`), reparse points and
  junctions on the path, and PROTECTED roots: the Coworker config and store
  folders, `browser-profile`, `.ssh`, `.aws`, `.gnupg`, Windows credential
  stores, and `Windows` / `Program Files` for writes. Also refuses files whose
  name matches the blocked list (`password`, `.env`, `.pem`, `wallet`, …).
- `policy/prohibited.py`: `scan_args(args: dict) -> str | None` returns
  `prohibited_card` (Luhn-valid 13–19 digit run), `prohibited_secret` (key shapes:
  `sk-`, `ghp_`, `AKIA`, `xox`, `-----BEGIN`, `Bearer `). Also
  `redact(text) -> str` for logs and clipboard reads.
- `policy/urls.py`: `check_url(url: str) -> str | None`. Scheme must be http or
  https; no userinfo; no `file:`; host not localhost, not private, link-local or
  metadata address (`169.254.169.254`), checked on the literal host and on
  resolved addresses via `resolve_and_pin(host) -> list[str]` helper.
- `policy/shell_rules.py`: `READONLY: dict[str, ReadonlyCommand]` (fixed ids with
  argv templates, never shell=True), `hard_deny(text) -> str | None` (format,
  diskpart, bcdedit, reg delete/add, `shutdown` in automatic mode, permanent
  delete forms, `Remove-Item -Recurse`, `cipher /w`, `sdelete`, `vssadmin delete`,
  `netsh advfirewall` changes, `Set-MpPreference`, `-EncodedCommand`,
  `Invoke-Expression`, `iex`, `DownloadString`, `Start-Process -Verb RunAs`).

## 5. Store (`store/`)

`Store(path: Path)` — SQLite in WAL mode, foreign keys on, `busy_timeout` 5 s,
thread-safe via one connection per thread plus a write lock.

```
kv_get(key, default=None) / kv_set(key, value)              # JSON values
turn_add(chat_id, role, content) / turns_recent(chat_id, limit=14) / turns_clear(chat_id)
summary_get(chat_id) -> str / summary_set(chat_id, text)
fact_add(chat_id, text, *, kind="note", untrusted=False) -> int
facts_list(chat_id, limit=60) -> list[dict]
facts_search(chat_id, query, limit=10) -> list[dict]        # FTS5, LIKE fallback
fact_forget(chat_id, needle) -> int
delivered_add(chat_id, path, name) / delivered_recent(chat_id, limit=25) -> list[dict]
approval_create(chat_id, tool, args, summary, provenance, autonomy, generation,
                two_channel, ttl_s) -> Approval
approval_get(approval_id) -> Approval | None
approval_consume(approval_id, nonce, actor_id, owner_id, *, now=None) -> Approval | None
approval_local_approve(approval_id) -> bool
approval_cancel_pending(reason) -> int / approvals_expire(now=None) -> int
approvals_pending(chat_id) -> list[Approval]
audit_intent(turn_id, actor, tool, tier, decision, code, args_summary, provider) -> int
audit_outcome(intent_id, ok, code, summary) -> None
audit_tail(limit=20) -> list[dict] / audit_verify() -> (bool, int | None)
audit_orphans() -> list[int]
reminder_add(chat_id, text, due_ts, repeat=None) -> int
reminders_due(now_ts) -> list[dict] / reminder_done(id, next_due_ts=None) -> None
reminders_list(chat_id) -> list[dict] / reminder_cancel(chat_id, id) -> bool
job_add(chat_id, name, instruction, schedule: dict, next_run_ts) -> int
jobs_due(now_ts) -> list[dict] / job_update(id, **fields) -> None
jobs_list(chat_id) -> list[dict] / job_disable(chat_id, id) -> bool
note_add(chat_id, title, body) -> int / notes_search(chat_id, query, limit=10) -> list[dict]
notes_list(chat_id, limit=20) -> list[dict]
task_add(chat_id, text, due_ts=None) -> int / tasks_list(chat_id, open_only=True) -> list[dict]
task_done(chat_id, id) -> bool
```

`Approval` (dataclass): `id, nonce, chat_id, tool, args, summary, provenance,
autonomy, generation, status, two_channel, local_ok, created_at, expires_at`.
Statuses: `pending, approved, denied, expired, cancelled, done`.

Audit tables are append-only: triggers `RAISE` on UPDATE or DELETE, except the
outcome column of an intent row, which is set once. Each row carries
`prev_hash` and `hash` (SHA-256 over the canonical row plus `prev_hash`), and an
HMAC key from keyring makes truncation detectable. Clipboard and typed text are
recorded as length and sha256 only. `args_summary` is at most 512 characters and
has secrets redacted.

Secrets (`store/secrets.py`): `get_secret(name) -> str | None` reads keyring
service `Coworker`, falling back to env `COWORKER_<NAME>`; `set_secret(name, value)`.
Config JSON never stores secrets. `Config.save()` drops them.

## 6. Safety (`safety/`)

- `approvals.py`: `ApprovalBroker(store)`. `propose(chat_id, spec, args, verdict,
  ctx) -> Approval` stores the frozen args and the provenance snapshot.
  `buttons(approval) -> list[list[dict]]` builds callback data
  `ap:<id10>:<nonce8>:y|n` (24 bytes max). `consume(approval_id, nonce, actor_id,
  owner_id) -> Approval | None` is one atomic compare-and-set; a two-channel
  approval also needs `local_ok`. TTL: 5 minutes interactive, 30 minutes unattended.
- `killswitch.py`: `KillSwitch(store)`. `generation` is persisted in kv.
  `register(token: CancelToken)` / `unregister(token)`. `stop()` bumps the
  generation, cancels every registered token, cancels pending approvals and
  drops queued work. `panic()` persists `panic=true`, pauses the scheduler and
  cancels approvals. `resume_local() -> bool` clears panic; it has no Telegram
  entry point. `is_panic() -> bool`.
- `pairing.py`: `Pairing(store)`. `issue_code(now=None) -> str` returns an 8-digit
  code shown only in the desktop UI or headless console, stored as a salted hash
  with a 10-minute TTL. `redeem(code, from_id, chat_id, chat_type, now=None) ->
  tuple[bool, str]`. Five failed attempts per code; a global lockout after 20
  failures in an hour, persisted. The first successful redemption pins
  `owner_user_id` and `owner_chat_id` in kv. Re-binding is local only.
  `owner() -> tuple[int, int] | None`.

## 7. Governor and budgets (`governor/`)

`Governor(os_port: OsPort, *, limits=None, monotonic=time.monotonic)`:

- `async run(gov_class, fn, *, timeout_s, cancel=None) -> Any` runs `fn` in a
  worker thread under the class's pool, after checking pressure. Raises
  `Refused(code)` (`throttled`, `low_memory`, `locked_desktop`, `paused`),
  `GovTimeout` (`timeout`) or `Cancelled`.
- Pools: `TOOL` 4 concurrent; `NET` 4; `LLM` 2; `UIA` 1 (serialised thread);
  `INPUT` 1 lease (refused when the desktop is locked); `VISION` 1; `BROWSER` 1;
  `OFFICE` 1; `SHELL` 1; `INDEX` 1 at idle priority; `STT` 1. Waiters are capped
  at 8 per pool; more fail fast with `throttled`.
- Pressure: pause background classes (`INDEX`, `VISION`, `STT`) when owner input
  was seen within 5 s, battery saver is on, free RAM is under 1.5 GB, or CPU mean
  is above 80% over 10 s; pause everything except interactive turns above 95%.
  Unplugged under 25% pauses background classes. Resume after 30 s clear
  (hysteresis). Non-interactive jobs are refused under 800 MB free RAM.
- Abandon rule: a timed-out in-process `UIA`, `INPUT`, `VISION` or `STT` job is
  marked abandoned; its result is dropped and the action is recorded as unknown.
  Three abandoned jobs disable that pool until restart.
- Children (shell, browser, office): a subprocess with stdout and stderr capped at
  16 KB each, a scrubbed environment (no names containing TOKEN, KEY, SECRET, PASS,
  CREDENTIAL, API, TELEGRAM, OPENAI, ANTHROPIC, DEEPSEEK), cwd in the scratch dir,
  killed as a process tree on timeout (`taskkill /T /F`), heartbeat from long
  workers, and `CREATE_NO_WINDOW`. Job Object memory caps are deferred.
- `status() -> dict` and `pressure() -> dict` feed the tray and `/status`.

`Budget(store, *, clock=time.time)` (day-local):

- `admit(tool, family, tier, actor, chat_id) -> Verdict` charges a weighted
  ceiling of 600 units per local day (READ 1, LOCAL_WRITE 3, DESTRUCTIVE 10,
  OUTBOUND 10, SYSTEM_CHANGE 10, shell 10, vision 5, LLM round 1), counted caps
  (DESTRUCTIVE 30, OUTBOUND 20 excluding owner-chat sends, SYSTEM_CHANGE 30,
  free-text shell 10, LLM rounds 1500), and per-minute rate buckets (READ 30,
  LOCAL_WRITE 10, UI input 20, browser 20, OUTBOUND 6, SYSTEM_CHANGE 6, shell 6,
  vision 6, all tools 60, LLM 30, Telegram sends 20 per chat). Exhaustion returns
  DENY `budget_exceeded` or `rate_limited`.
- `admit_bytes(n, chat_id) -> Verdict` enforces 500 MB per day to the owner chat.
- `usage() -> dict` and `warning_due() -> bool` (80% of ceiling, once per day).

## 8. File index (`fileindex/`)

`FileIndex(db_path: Path, roots: list[str] | None = None)`. Default roots are the
user folders (Desktop, Documents, Downloads, Pictures, Music, Videos) and their
OneDrive equivalents, never whole disks. The crawl runs at IDLE priority in the
`INDEX` pool with explicit stacks (no recursion), depth 10, junctions and symlinks
not followed, a visited set keyed by normalised path, and skips for
`node_modules`, `.git`, `__pycache__`, `site-packages`, `AppData`, `Windows`.
Placeholder files (OFFLINE, RECALL_ON_OPEN, RECALL_ON_DATA_ACCESS) are never opened;
they are indexed by name only. Text is extracted for files up to 45 MB and stored
up to 200 KB per file. Budgets: 2 GB database, 500 000 rows, 500 MB of writes per
day, 100 MB per hour; commit every 200 rows.

- `search(query, limit=12, root=None) -> dict` returns
  `{results: [{name, path, size, mtime, kind, snippet?}], source: "index"|"live", truncated}`.
  Live fallback uses `fs.find_files` with an 8 s deadline and depth 7.
- `list_dir(path, limit=200) -> dict`.
- `start(stop_event)` / `stop()` / `stats() -> dict`.

## 9. Transport (`transport/`)

- `bot.py` `BotApi(token, *, client=None)` — synchronous `httpx.Client`. Methods:
  `get_updates(offset, timeout=25)`, `send_message(chat_id, text, buttons=None,
  reply_to=None)`, `send_document(chat_id, path, caption="")`, `send_photo(...)`,
  `send_chat_action(chat_id, action="typing")`, `answer_callback(id, text="")`,
  `edit_reply_markup(chat_id, message_id)`, `get_file_bytes(file_id, max_bytes=20MB)`,
  `delete_webhook()`, `set_my_commands(list)`. Every call returns a dict and never
  raises on a network error. The token is never logged; a logging filter redacts
  `bot<digits>:<hash>`.
- `outbox.py` `Outbox(api, store)` — `text(chat_id, text, buttons=None)`,
  `document(chat_id, path, caption)`, `ask(chat_id, question, options)`,
  `notify(text)`. Sends only to the owner's chat; any other chat id is refused.
  Text is chunked at 3500 characters.
- `gate.py` `OwnerGate(store)` — `allows(update) -> bool`. Accepts only
  `message` and `callback_query` where `from.id == owner_user_id` and
  `chat.type == "private"` and `chat.id == owner_chat_id`. Before pairing, only a
  `/connect <code>` message from any private chat is passed to `Pairing.redeem`.
  Everything else is dropped before parsing and audited as `gate_dropped`.
- `poll.py` `PollLoop(api, store, gate, handle)` — `run(stop_event)`. Persists the
  `getUpdates` offset in kv *before* handling a batch. Backoff from 1 s doubling to
  60 s. HTTP 409 stops polling and reports loudly. Calls `delete_webhook()` once at
  start. Handlers are idempotent per `update_id`.
- `commands.py` — `parse(text) -> (name, args)`. Commands: `/start`, `/help`,
  `/status`, `/stop`, `/panic`, `/forget`, `/reset`, `/facts`, `/jobs`, `/reminders`,
  `/audit`. `/resume` is not available over Telegram (local only).
- Voice notes: downloaded (max 20 MB), converted through `stt.py` under the `STT`
  pool, transcribed text tagged OWNER.

## 10. LLM (`llm_base.py`, `llm.py`, `llm_anthropic.py`)

```
@dataclass Msg:      role: "user"|"assistant"|"tool", content: str|None,
                     tool_calls: list[ToolCallReq] = [], tool_call_id: str = "", name: str = ""
@dataclass ToolCallReq: id: str, name: str, args: dict
@dataclass Turn:     text: str, tool_calls: list[ToolCallReq], stop_reason: str, usage: dict
class ChatProvider(Protocol):
    name: str
    dialect: str                       # "openai" | "anthropic"
    async def chat(system, messages, tools, *, max_tokens, temperature=0.2) -> Turn
    async def vision(prompt, image_b64, *, max_tokens=1200) -> str
    async def close() -> None
class ProviderError(RuntimeError)
```

`tools` is `Registry.openai_tools(...)` for `dialect == "openai"` and
`Registry.anthropic_tools(...)` for `"anthropic"`. The OpenAI adapter keeps the
presets and the `reasoning_content` salvage from the current `llm.py`. The
Anthropic adapter sends `max_tokens` (required), converts `tool_use` and
`tool_result` blocks, and sends images as base64 blocks. `build_provider(cfg,
secrets) -> ChatProvider` chooses the adapter from config.

## 11. Scheduler (`scheduler.py`)

`Scheduler(store, *, run_job, deliver, kill, clock=time.time)`:

- `tick(now=None)` runs every 30 s. Reminders are delivered as fixed text through
  `deliver(chat_id, text)`, with no model call. Jobs call
  `run_job(job) -> str` (the orchestrator, actor `scheduler`, autonomy capped at AR).
- Jobs have frozen plans. A job that fails three times in a row is paused.
  Missed runs outside a 2-hour window are skipped and reported.
- Panic pauses ticks; resume is local only.
- `start()` / `stop()` run the tick in a daemon thread.

## 12. Orchestrator and dispatch (wave 2)

`dispatch.invoke(name, args, turn) -> ToolResult` is the only way a tool runs:

1. look up the spec; unknown or not visible to the grants → DENY `unknown_tool`;
2. `validate_args` → `arg_invalid`;
3. build a `CallContext` snapshot from the turn state;
4. `PolicyKernel.evaluate`;
5. DENY → audit and return the error to the model;
   CONFIRM → `ApprovalBroker.propose`; the turn ends with a card;
6. ALLOW → `Budget.admit`, then `audit_intent`, `Governor.run`, `audit_outcome`;
7. untrusted output → turn becomes CONTENT and `content_norm` grows;
   `surfaced` paths are added to the turn's surfaced set.

Owner taps go through `ApprovalBroker.consume`, re-evaluate the kernel with the
frozen provenance, and run the handler only if the verdict is not DENY.

Turn state: `TurnState` is per turn and is never shared between chats. Two
interactive turns may run concurrently across chats, but never two in one chat.

## 13. Error codes

`unknown_tool`, `panic`, `not_granted`, `prohibited_tier`, `prohibited_card`,
`prohibited_secret`, `path_invalid`, `protected_path`, `reparse_point`,
`device_name`, `not_surfaced`, `origin_content`, `tier_default`, `relaxed`,
`taint_escalate`, `taint_unattended`, `hard_deny`, `budget_exceeded`,
`rate_limited`, `arg_invalid`, `policy_error`, `throttled`, `low_memory`,
`locked_desktop`, `paused`, `timeout`, `cancelled`, `url_refused`, `element_gone`,
`password_field`, `not_configured`, `close_requested`.

## 14. Test requirements

Every module has tests under `agent/tests/`. Tests use fakes: `FakeOs`,
`FakeProvider`, `FakeBotApi`, a temporary SQLite file per test. No test touches
the network, the real clipboard, real windows, or real keyring entries.
Windows-only paths are tested by pure functions where possible and marked
`@pytest.mark.windows` otherwise.

## 15. Changes after the first security review

A review of six attack surfaces confirmed 53 findings; a re-check confirmed five
more. All are fixed, each with a regression test. The contract above is amended
as follows.

**Tools and kernel**

- `ToolSpec.egress` marks a call that sends data off the machine (web fetch and
  search, browser open). After the turn has read local data (files, office,
  desktop, notes) an egress call needs the owner's confirmation, or is refused
  when unattended (`egress_after_local_read`, `egress_unattended`). Its URL and
  query are origin-checked once local data has been read.
- Relax hooks (for example a plain button click, or a volume change) apply only
  under ask-for-writes. Under ask-always they never turn a confirmation into a run.
- Origin checks compare every value as a whole and in 64-character windows, so a
  passage copied into a longer value is still caught.
- Taint escalation applies to metadata (file names, window titles, search hits)
  as well as to content.
- Card numbers are scanned after percent-decoding, across separators, and across
  calls within a turn (typed text is scanned together with the tail of what the
  turn typed before).
- Paths: any UNC spelling (`\host\share`) is refused before the operating system
  is asked anything. The config and store folders and browser credential stores
  are protected by folder, and the scratch folder is readable so that files the
  assistant generated can be sent.
- `file_delete` requires a path that the owner's own search returned (stricter than
  the table in section 3).
- Window titles and file names returned by listing and search are untrusted content.
- Permanent deletes in the shell (`del`, `rd`, `Remove-Item`, `.NET` delete calls,
  and the en or em dash variants of their switches) are hard-denied.

**Approvals, options and the runtime**

- An approved action is charged to the daily budget and runs with the provenance
  and owner words it was proposed with. Stopping the assistant cancels it too.
- Option buttons belong to the question that carried them. A tap claims that
  question's options once; a tap on a retired question does nothing. A tap keeps
  the provenance of the turn that asked, so options taken from content stay untrusted.
- Each owner message is handled one turn at a time per chat.
- A scheduled run that is denied by the budget counts as a failure, not a success.
- A model reply with neither text nor tool calls is reported as "no answer", not "done".

**Audit, pairing and the desktop**

- Audit rows hash every outcome column, and the chain head is anchored, so
  removing an outcome breaks verification. A failed keyring read never mints a
  replacement audit key.
- Pairing lockout is cleared only from the desktop ("Yangi kod"); Telegram cannot clear it.
- A 409 conflict from Telegram is shown as offline in the window.

**Elevation and browser**

- `is_elevated()` returns `None` when the probe fails. Shell commands are refused
  unless the answer is an explicit `False`.
- The browser proxy checks WebSocket traffic when the installed Playwright
  supports it (1.48 or later is required in `requirements.txt`).

**Known gaps, stated plainly**

- `note_add` and `task_add` store the text the model passes without a provenance
  flag, so a note written after reading a page is not marked untrusted. Only
  `remember` (facts) records it.
- `TurnState.sendable` is recorded but not yet used to push generated files to the
  owner automatically; the model sends them with `send_file`.
- Mail, calendar and speech output are not built.
