"""Pure helpers behind the desktop window.

No Tk window is created here. The tests call the helpers in coworker.ui_panels and
check their wording, state mapping and redaction. One test imports coworker.ui in a
child process and checks that the import opens no window and loads no runtime.
"""
from __future__ import annotations

import logging
import queue
import subprocess
import sys
import time
from pathlib import Path

from coworker import ui_panels as panels
from coworker.store.approval_rows import Approval
from coworker.tools.registry import FAMILIES

AGENT_DIR = Path(__file__).resolve().parent.parent
FAKE_TOKEN = "123456789:" + "A" * 35
FAKE_CARD = "4111 1111 1111 1111"  # a Luhn-valid test number, so the redactor treats it as a card


def _approval(**overrides) -> Approval:
    now = time.time()
    fields = dict(
        id="abcdefghij", nonce="12345678", chat_id=5, tool="files.write",
        args={"path": "C:/private/plan.docx"}, summary="«plan.docx» faylini o'zgartirish",
        provenance=1, autonomy="ask_for_writes", generation=0, status="pending",
        two_channel=False, local_ok=False, created_at=now, expires_at=now + 600,
    )
    fields.update(overrides)
    return Approval(**fields)


# ------------------------------------------------------------- autonomy

def test_each_autonomy_level_has_its_own_explanation():
    texts = [panels.autonomy_explanation(level) for level in panels.AUTONOMY_LEVELS]
    assert all(texts)
    assert len(set(texts)) == len(panels.AUTONOMY_LEVELS)


def test_autonomy_labels_and_help_cover_the_three_levels():
    assert set(panels.AUTONOMY_LABELS) == set(panels.AUTONOMY_LEVELS)
    assert set(panels.AUTONOMY_HELP) == set(panels.AUTONOMY_LEVELS)


def test_default_autonomy_is_ask_for_writes():
    assert panels.DEFAULT_AUTONOMY == "ask_for_writes"
    assert panels.normalize_autonomy(None) == "ask_for_writes"


def test_unknown_autonomy_falls_back_to_the_default():
    assert panels.normalize_autonomy("bogus") == "ask_for_writes"
    assert panels.normalize_autonomy("panic") == "ask_for_writes"
    assert panels.autonomy_explanation("panic") == panels.autonomy_explanation("ask_for_writes")


def test_known_autonomy_is_kept():
    assert panels.normalize_autonomy(" autonomous_readonly ") == "autonomous_readonly"


# ------------------------------------------------------------- families

def test_every_family_has_a_label():
    assert set(panels.FAMILY_LABELS) == set(FAMILIES)


def test_family_checkbox_state_is_the_inverse_of_the_disabled_list():
    states = panels.family_states(["shell", "mail"])
    assert states["shell"] is False
    assert states["mail"] is False
    assert states["files"] is True
    assert set(states) == set(FAMILIES)


def test_unknown_names_in_the_disabled_list_do_not_change_the_boxes():
    assert all(panels.family_states(["no_such_family"]).values())


def test_unchecked_boxes_become_the_disabled_list():
    states = panels.family_states([])
    states["shell"] = False
    states["web"] = False
    assert panels.disabled_from_states(states) == ["shell", "web"]


def test_disabled_list_survives_a_round_trip():
    disabled = ["apps", "web"]
    states = panels.family_states(disabled)
    assert panels.disabled_from_states(states) == sorted(disabled)


def test_names_this_window_does_not_know_are_kept_on_save():
    states = panels.family_states([])
    states["shell"] = True  # checked again, so it leaves the list
    saved = panels.disabled_from_states(states, previous=["legacy_thing", "shell"])
    assert saved == ["legacy_thing"]


def test_family_label_falls_back_to_the_raw_name():
    assert panels.family_label("brand_new") == "brand_new"


# ------------------------------------------------------------- LLM presets

def test_endpoint_and_model_pick_the_preset():
    assert panels.preset_for("https://api.deepseek.com", "deepseek-chat") == "DeepSeek"


def test_claude_presets_share_a_host_and_the_model_breaks_the_tie():
    assert panels.preset_for("https://api.anthropic.com", "claude-haiku-5-5") == "Claude Haiku 5.5"
    assert panels.preset_for("https://api.anthropic.com", "claude-sonnet-5-5") == "Claude Sonnet 5.5"


def test_custom_endpoint_has_no_preset():
    assert panels.preset_for("http://example.test/v1", "some-model") == ""


def test_trailing_slash_does_not_hide_a_preset():
    assert panels.preset_for("https://api.deepseek.com/", "") == "DeepSeek"


# ------------------------------------------------------------- approvals

def test_approval_text_shows_the_summary_not_the_arguments():
    text = panels.approval_text(_approval())
    assert "plan.docx" in text
    assert "C:/private" not in text


def test_two_channel_row_asks_for_the_local_approval():
    row = _approval(two_channel=True, local_ok=False)
    assert panels.needs_local_approval(row) is True
    assert "mahalliy" in panels.approval_channel(row).lower()


def test_local_approval_already_given_needs_no_second_press():
    row = _approval(two_channel=True, local_ok=True)
    assert panels.needs_local_approval(row) is False
    assert "Mahalliy tasdiq berildi" in panels.approval_channel(row)


def test_one_channel_row_is_read_only_and_points_to_telegram():
    row = _approval(two_channel=False)
    assert panels.needs_local_approval(row) is False
    assert "Telegram" in panels.approval_channel(row)


def test_expired_approvals_are_filtered_out():
    now = time.time()
    live = _approval(id="live000001", expires_at=now + 60)
    stale = _approval(id="stale00001", expires_at=now - 1)
    assert [a.id for a in panels.unexpired([live, stale], now=now)] == ["live000001"]


# ------------------------------------------------------------- audit

def _audit(**overrides) -> dict:
    row = {
        "ts": 1_700_000_000.0, "tool": "files.write", "tier": "LOCAL_WRITE",
        "decision": "CONFIRM", "code": "tier_default", "args_summary": "to C:/x.txt",
        "outcome_ok": None, "outcome_code": None,
    }
    row.update(overrides)
    return row


def test_audit_row_says_when_there_is_no_outcome_yet():
    assert "natija yo'q" in panels.format_audit_row(_audit(outcome_ok=None))


def test_audit_row_shows_success_and_failure_codes():
    assert "bajarildi" in panels.format_audit_row(_audit(outcome_ok=True))
    failed = panels.format_audit_row(_audit(outcome_ok=False, outcome_code="timeout"))
    assert "xato: timeout" in failed


def test_audit_row_is_redacted_and_kept_on_one_line():
    row = _audit(args_summary=f"card {FAKE_CARD}\nand token {FAKE_TOKEN}")
    text = panels.format_audit_row(row)
    assert FAKE_CARD not in text
    assert FAKE_TOKEN not in text
    assert "\n" not in text


def test_verify_text_reports_ok_and_the_first_bad_row():
    assert "buzilmagan" in panels.verify_text((True, None))
    assert "#7" in panels.verify_text((False, 7))


# ------------------------------------------------------------- header status

def test_online_status_hides_the_pairing_detail():
    colour, text = panels.status_line("online", "ulash kodi: 12345678")
    assert text == "Ulangan"
    assert "12345678" not in text


def test_offline_status_shows_its_reason():
    _, text = panels.status_line("offline", "Telegram tokeni kiritilmagan")
    assert "Telegram tokeni kiritilmagan" in text


def test_panic_overrides_every_other_state():
    _, text = panels.status_line("online", "", panic=True)
    assert text.startswith("PANIC")


def test_working_status_shows_its_detail():
    _, text = panels.status_line("working", "fayl qidirilmoqda")
    assert text == "Ishlayapti: fayl qidirilmoqda"


def test_unknown_state_is_shown_as_it_is():
    assert panels.status_line("weird", "")[1] == "weird"


def test_connect_command_format():
    assert panels.connect_command("12345678") == "/connect 12345678"


def test_secret_hint_never_carries_a_value():
    assert "holat" in panels.secret_hint(True)
    assert "kiritilmagan" in panels.secret_hint(False)


# ------------------------------------------------------------- redaction and the journal

def test_redact_line_removes_bot_tokens_and_cards():
    text = panels.redact_line(f"poll failed for bot{FAKE_TOKEN} card {FAKE_CARD}")
    assert FAKE_TOKEN not in text
    assert FAKE_CARD not in text


def test_redact_line_removes_a_bare_token_too():
    assert FAKE_TOKEN not in panels.redact_line(f"token={FAKE_TOKEN}")


def test_journal_handler_sends_only_redacted_lines():
    handler = panels.QueueLogHandler()
    record = logging.LogRecord(
        "transport.bot", logging.INFO, __file__, 1,
        "token %s card %s", (FAKE_TOKEN, FAKE_CARD), None,
    )
    handler.emit(record)
    line = handler.lines.get_nowait()
    assert FAKE_TOKEN not in line
    assert FAKE_CARD not in line
    assert "transport.bot" in line


def test_journal_handler_drops_lines_when_full_without_raising():
    handler = panels.QueueLogHandler(maxsize=1)
    for index in range(3):
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "line %s", (index,), None)
        handler.emit(record)
    assert handler.lines.qsize() == 1


def test_drain_queue_takes_everything_then_stops():
    items: queue.Queue = queue.Queue()
    for value in range(5):
        items.put(value)
    assert panels.drain_queue(items, limit=3) == [0, 1, 2]
    assert panels.drain_queue(items) == [3, 4]
    assert panels.drain_queue(items) == []


# ------------------------------------------------------------- import is window-free

def test_importing_the_window_module_opens_no_window_and_loads_no_runtime():
    code = (
        "import sys; import coworker.ui; "
        "print(any(m.startswith('coworker.runtime') for m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(AGENT_DIR), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
