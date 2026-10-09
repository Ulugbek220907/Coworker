"""Shell tools: a fixed table of read-only commands, and owner-confirmed PowerShell.

The two tools are trusted differently. ``shell_readonly`` runs only the entries of
``policy.shell_rules.READONLY``: the model picks an id and fills the placeholders that
entry declares, and never supplies a program name or a flag, so a read-only entry
cannot become a different program. ``shell_run`` runs arbitrary PowerShell, which is
SYSTEM_CHANGE. The kernel asks for a confirmation card; this module adds the checks the
kernel cannot make: the standing hard-deny rules (on the raw text and on a squashed
form), a refusal while Coworker runs elevated, and a refusal when the command came from
content rather than from the owner.

Every child goes through ``run_child``: a list argv with no shell, a scrubbed
environment, the scratch folder as working folder, 16 KB per output stream, a 30 s
limit, and a kill of the whole process tree when the limit is hit. Children start with
CREATE_NO_WINDOW so nothing flashes on the owner's screen.

From ``policy.shell_rules`` this module uses ``READONLY``, ``hard_deny`` and the path
rules only. A ``READONLY`` entry is a ``ReadonlyCommand``: a fixed argv with at most one
slot, and a validator for the one value that may fill it.
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Mapping

from ..config import config_dir
from ..core.types import CallContext, Provenance, Tier, ToolResult, Verdict, normalize_text
from ..policy import paths
from ..policy.shell_rules import READONLY, hard_deny
from .registry import Services, ToolCall, ToolSpec

log = logging.getLogger("shell")

CAP_BYTES = 16 * 1024
TIMEOUT_S = 30.0
# The governor's limit sits above the helper's, so the helper kills the tree before
# the governor gives up on the handler.
HANDLER_LIMIT_S = TIMEOUT_S + 5
KILL_WAIT_S = 5.0
READER_JOIN_S = 2.0
_READ_CHUNK = 4096
# The confirmation card must show the whole command. The kernel cuts summaries at 600
# characters, so the command and the folder must fit in that with room to spare.
MAX_COMMAND = 300
MAX_CWD = 200
MAX_ARG = 260                 # the one value a read-only command takes
SCRUBBED_MARKERS = (
    "TOKEN", "KEY", "SECRET", "PASS", "CREDENTIAL", "API",
    "TELEGRAM", "OPENAI", "ANTHROPIC", "DEEPSEEK",
)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_POWERSHELL = ("powershell", "-NoProfile", "-NonInteractive", "-Command")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# A folder that starts with a drive letter. A UNC share ("\\server\share") does not match,
# so it is refused before any filesystem call can reach the network.
_DRIVE_FOLDER = re.compile(r"[A-Za-z]:[\\/][^\x00-\x1f]*")
# Characters PowerShell and cmd ignore when they read a word: `F`ORMAT`, "format", fo^rmat.
_ESCAPES = re.compile(r"[`^\"']")
_BLANKS = re.compile(r"\s+")


# ------------------------------------------------------------------ child process

@dataclass(frozen=True)
class ChildResult:
    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool


class _Capture(threading.Thread):
    """Reads one pipe to its end, keeping the first CAP_BYTES.

    The rest is read and dropped rather than left unread: a child that fills an unread
    pipe blocks, and then only the timeout could end it.
    """

    def __init__(self, pipe: IO[bytes]) -> None:
        super().__init__(daemon=True)
        self._pipe = pipe
        self._kept = bytearray()
        self._total = 0

    def run(self) -> None:
        try:
            while chunk := self._pipe.read1(_READ_CHUNK):
                self._total += len(chunk)
                room = CAP_BYTES - len(self._kept)
                if room > 0:
                    self._kept += chunk[:room]
        finally:
            self._pipe.close()

    @property
    def data(self) -> bytes:
        return bytes(self._kept)

    @property
    def truncated(self) -> bool:
        return self._total > CAP_BYTES


def run_child(argv: list[str], *, cwd: Path, timeout_s: float = TIMEOUT_S) -> ChildResult:
    """Run one program with a list argv, no shell, and the output and time limits applied.

    Output written before a timeout is still returned. Raises OSError when the program
    cannot be started, and ValueError for an empty argv or a program name with folder parts.
    """
    if not argv:
        raise ValueError("empty argv")
    proc = subprocess.Popen(
        [_resolve_exe(argv[0]), *argv[1:]],
        cwd=str(cwd),
        env=scrubbed_env(),
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    out = _Capture(proc.stdout)  # type: ignore[arg-type]
    err = _Capture(proc.stderr)  # type: ignore[arg-type]
    out.start()
    err.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            proc.wait(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            log.error("pid %d did not exit after it was killed", proc.pid)
    out.join(READER_JOIN_S)
    err.join(READER_JOIN_S)
    return ChildResult(
        exit_code=proc.returncode,
        stdout=_decode(out.data),
        stderr=_decode(err.data),
        truncated=out.truncated or err.truncated,
        timed_out=timed_out,
    )


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop the child and every process it started.

    taskkill /T walks the process tree; a plain kill of the root would leave a
    PowerShell that the command started running after the timeout.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                [_resolve_exe("taskkill"), "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=KILL_WAIT_S, check=False, shell=False,
                creationflags=_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            log.warning("taskkill could not finish for pid %d", proc.pid)
    proc.kill()


def _resolve_exe(name: str) -> str:
    """The program a child starts. Bare names are looked up on PATH only.

    CreateProcess searches the calling folder before PATH, and every child's working
    folder is the scratch folder, where a file named like a system tool could be left.
    Names with folder parts are refused for the same reason.
    """
    if os.path.isabs(name):
        return name
    if os.path.basename(name) != name:
        raise ValueError(f"not a bare program name: {name}")
    exts = [e for e in os.environ.get("PATHEXT", ".EXE").split(";") if e] if os.name == "nt" else [""]
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        # A relative PATH entry would be the process folder, which is not searched.
        if not folder or not os.path.isabs(folder):
            continue
        for ext in exts:
            candidate = os.path.join(folder, name + ext)
            if os.path.isfile(candidate):
                return candidate
    raise FileNotFoundError(f"{name} is not on PATH")


def scrubbed_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment without secret-shaped names.

    Matching is on the upper-cased name, so ``openai_api_key`` is dropped like
    ``OPENAI_API_KEY``. Children never need the owner's keys, and a command that prints
    its environment must not reveal them.
    """
    env = os.environ if source is None else source
    return {k: v for k, v in env.items() if not any(m in k.upper() for m in SCRUBBED_MARKERS)}


def scratch_dir() -> Path:
    """The folder every child runs in: COWORKER_HOME/scratch, created on first use."""
    folder = config_dir() / "scratch"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _decode(raw: bytes) -> str:
    return raw.decode(_console_codepage(), errors="replace")


def _console_codepage() -> str:
    """Console programs write in the OEM code page (cp866 or cp437 on most machines), not the ANSI one."""
    return f"cp{ctypes.windll.kernel32.GetOEMCP()}" if os.name == "nt" else "utf-8"


# ------------------------------------------------------------------ read-only table

def _unsafe_arg(args: dict) -> str | None:
    """Values that could change how a program reads its arguments.

    Control characters are refused outright. A string beginning with ``-`` or ``/`` is
    refused because the program would read it as an option, which the table never
    intends. Integers are fine, so a negative number passes.
    """
    for key, value in args.items():
        if not isinstance(value, str):
            continue
        if _CONTROL.search(value):
            return f"{key} contains a control character"
        if value.startswith(("-", "/")):
            return f"{key} must not start with '-' or '/': it would be read as an option"
    return None


def _outcome(result: ChildResult, **extra: Any) -> ToolResult:
    data: dict[str, Any] = {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "truncated": result.truncated,
        **extra,
    }
    if result.timed_out:
        return ToolResult(ok=False, data=data, code="timeout", untrusted=True,
                          error=f"buyruq {TIMEOUT_S:g} soniyadan ko'p ishladi va to'xtatildi")
    if result.exit_code != 0:
        return ToolResult(ok=False, data=data, untrusted=True,
                          error=f"buyruq xato kodi {result.exit_code} bilan tugadi")
    return ToolResult(ok=True, data=data, untrusted=True)


def _execute(argv: list[str], folder: Path | None, **extra: Any) -> ToolResult:
    try:
        result = run_child(argv, cwd=folder if folder is not None else scratch_dir())
    except (OSError, ValueError) as exc:
        return ToolResult.fail("not_configured", f"buyruqni boshlab bo'lmadi: {exc}")
    return _outcome(result, **extra)


def _shell_readonly(call: ToolCall) -> ToolResult:
    """Run one table entry. The entry's own validator decides whether its one value is acceptable."""
    command_id = call.args.get("command_id")
    entry = READONLY.get(command_id) if isinstance(command_id, str) else None
    if entry is None:
        return ToolResult.fail("arg_invalid", f"buyruq ro'yxatda yo'q: {command_id}")
    args = call.args.get("args", {})
    if not isinstance(args, dict) or set(args) - {"arg"}:
        return ToolResult.fail("arg_invalid", "args faqat 'arg' maydonini olishi mumkin")
    value = args.get("arg")
    if value is not None and (not isinstance(value, str) or len(value) > MAX_ARG or _unsafe_arg(args)):
        return ToolResult.fail("arg_invalid", "argument qiymati noto'g'ri")
    argv = entry.build(value)
    if argv is None:
        return ToolResult.fail("arg_invalid", f"{command_id} buyrug'i uchun argument yo'q yoki noto'g'ri")
    return _execute(argv, None, command_id=command_id)


# ------------------------------------------------------------------ free-text PowerShell

def _squash(command: str) -> str:
    """The command with the characters PowerShell and cmd ignore removed, in lower case.

    ``F`ORMAT``, ``"format"`` and ``fo^rmat`` all run as ``format``, so the rules see
    this form as well as the raw text. It is a pattern aid, not a parser: string joins
    such as ``'for'+'mat'`` still reach the owner's card.
    """
    return _BLANKS.sub(" ", _ESCAPES.sub("", command)).strip().lower()


def _is_folder(value: str) -> bool:
    path = Path(value)
    return path.is_absolute() and path.is_dir()


def _card(args: dict) -> str:
    cwd = args.get("cwd") or "vaqtinchalik papka (scratch)"
    return f"PowerShell buyrug'i:\n{args.get('command', '')}\nIsh papkasi: {cwd}"


def _check_hard_deny(args: dict, ctx: CallContext, svc: Services | None) -> Verdict | None:
    command = str(args.get("command", ""))
    for text in (command, _squash(command)):
        rule = hard_deny(text)
        if rule is not None:
            return Verdict.deny("hard_deny", f"refused by a standing rule: {rule}")
    return None


def _check_not_elevated(args: dict, ctx: CallContext, svc: Services | None) -> Verdict | None:
    """Allowed only on an explicit "not elevated". Any other answer, unknown included, refuses.

    The port answers None when the probe itself fails; a probe that failed is not
    evidence that Coworker is unelevated, and the refusal says so.
    """
    port = getattr(svc, "os", None)
    elevated = port.is_elevated() if port is not None else None
    if elevated is False:
        return None
    return Verdict.deny(
        "elevated_refused",
        "shell_run is refused while Coworker runs elevated or its elevation is unknown",
    )


def _check_cwd(args: dict, ctx: CallContext, svc: Services | None) -> Verdict | None:
    """The folder must be a drive-letter path that the file rules accept.

    This runs before any filesystem call on the folder. The kernel's own path check
    would touch a UNC share (\\\\server\\share) with lstat and realpath, which can
    start an authenticated network connection before the owner has seen the card,
    so the shape is judged first and the rules only for a local path.
    """
    cwd = args.get("cwd")
    if cwd is None:
        return None
    if not isinstance(cwd, str) or _DRIVE_FOLDER.fullmatch(cwd) is None:
        return Verdict.deny("path_invalid", "the folder must start with a drive letter, such as C:\\")
    code = paths.check_path(cwd, write=False)
    if code is not None:
        return Verdict.deny(code, "the folder is refused by the path rules")
    return None


def _check_confirm(args: dict, ctx: CallContext, svc: Services | None) -> Verdict | None:
    return Verdict.confirm(
        "shell_confirm",
        "free-text shell commands always need the owner's confirmation",
        summary=_card(args),
        two_channel=True,
    )


def _shell_run(call: ToolCall) -> ToolResult:
    command = call.args.get("command", "")
    if not command.strip():
        return ToolResult.fail("arg_invalid", "buyruq bo'sh")
    # Second line of defence: the kernel already checked origin for the first call, but
    # a command copied from a page must never run because the owner tapped a card later.
    if call.ctx.provenance == Provenance.CONTENT and normalize_text(command) not in call.ctx.owner_norm:
        return ToolResult.fail("origin_content", "buyruq sizning xabaringizda yo'q; u hujjat yoki sahifadan olingan")
    cwd = call.args.get("cwd")
    if cwd is not None and not _is_folder(cwd):
        return ToolResult.fail("arg_invalid", "ish papkasi to'liq yo'l bilan ko'rsatilgan mavjud papka bo'lishi kerak")
    return _execute([*_POWERSHELL, command], Path(cwd) if cwd is not None else None)


# ------------------------------------------------------------------ registration

_READONLY_PARAMS = {
    "type": "object",
    "properties": {
        "command_id": {"type": "string", "enum": sorted(READONLY)},
        "args": {
            "type": "object",
            "properties": {"arg": {"type": "string", "maxLength": MAX_ARG}},
        },
    },
    "required": ["command_id"],
}

_SHELL_RUN_PARAMS = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "maxLength": MAX_COMMAND},
        "cwd": {"type": "string", "maxLength": MAX_CWD},
    },
    "required": ["command"],
}

SPECS: list[ToolSpec] = [
    ToolSpec(
        name="shell_readonly",
        family="shell",
        tier=Tier.READ,
        description=(
            "Run one fixed read-only command. command_id must be one of the listed ids. Only some ids take "
            "one value, given as args {\"arg\": ...}; the others take no args. The output is text from this "
            "machine and is untrusted."
        ),
        parameters=_READONLY_PARAMS,
        handler=_shell_readonly,
        gov_class="SHELL",
        timeout_s=HANDLER_LIMIT_S,
        untrusted=True,
    ),
    ToolSpec(
        name="shell_run",
        family="shell",
        tier=Tier.SYSTEM_CHANGE,
        description=(
            "Run a PowerShell command. The owner confirms a card that shows the exact command and folder, and "
            "the laptop must approve it locally. Refused while Coworker runs elevated. Use only when no "
            "read-only command fits."
        ),
        parameters=_SHELL_RUN_PARAMS,
        handler=_shell_run,
        gov_class="SHELL",
        timeout_s=HANDLER_LIMIT_S,
        untrusted=True,
        reversible=False,
        sensitive_args=("command",),
        # No path_args: the kernel's path check would run before _check_cwd and touch a UNC share.
        arg_checks=(_check_cwd, _check_hard_deny, _check_not_elevated, _check_confirm),
        summary=_card,
    ),
]
