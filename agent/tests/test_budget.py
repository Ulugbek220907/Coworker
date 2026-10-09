"""Budget: weighted day ceiling, counted caps, rate buckets, bytes, warning and rollover."""
from __future__ import annotations

import threading
from datetime import datetime

import pytest

from coworker.core.types import Decision, Tier
from coworker.governor.budget import BYTES_PER_DAY, DAILY_UNITS, Budget


class KvStore:
    """The two kv methods the budget uses, backed by a dict."""

    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    def kv_get(self, key: str, default: object = None) -> object:
        return self.data.get(key, default)

    def kv_set(self, key: str, value: object) -> None:
        self.data[key] = value


class Clock:
    def __init__(self, when: datetime) -> None:
        self.now = when.timestamp()   # a naive datetime is local time, which is what the budget uses

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def set(self, when: datetime) -> None:
        self.now = when.timestamp()


DAY = "2026-10-08"
NEXT_DAY = "2026-10-09"


def at(day: str, hour: int = 12, minute: int = 0) -> datetime:
    year, month, date = (int(part) for part in day.split("-"))
    return datetime(year, month, date, hour, minute)


def units_key(day: str = DAY) -> str:
    return f"budget:{day}:units"


def make(day: str = DAY, hour: int = 12, minute: int = 0) -> tuple[Budget, KvStore, Clock]:
    store = KvStore()
    clock = Clock(at(day, hour, minute))
    return Budget(store, clock=clock), store, clock


def allowed(verdict) -> bool:
    return verdict.decision is Decision.ALLOW


def used(budget: Budget) -> int:
    return budget.usage()["units"]["used"]


# ------------------------------------------------------------ weights

@pytest.mark.parametrize("tool, family, tier, cost", [
    pytest.param("list_dir", "files", Tier.READ, 1, id="read"),
    pytest.param("file_copy", "files", Tier.LOCAL_WRITE, 3, id="local_write"),
    pytest.param("file_delete", "files", Tier.DESTRUCTIVE, 10, id="destructive"),
    pytest.param("send_file", "telegram", Tier.OUTBOUND, 10, id="outbound"),
    pytest.param("shell_run", "shell", Tier.SYSTEM_CHANGE, 10, id="system_change_shell"),
    pytest.param("shell_readonly", "shell", Tier.READ, 10, id="readonly_shell_costs_shell_weight"),
    pytest.param("screen_read", "desktop", Tier.READ, 5, id="vision"),
    pytest.param("web_screenshot", "browser", Tier.READ, 5, id="browser_vision"),
    pytest.param("llm_round", "llm", Tier.READ, 1, id="llm_round"),
])
def test_each_action_costs_its_weight(tool, family, tier, cost):
    budget, _, _ = make()
    assert allowed(budget.admit(tool, family, tier, "owner", None))
    assert used(budget) == cost


def test_a_plain_string_tier_is_classified_like_the_enum():
    budget, _, _ = make()
    assert allowed(budget.admit("file_delete", "files", "DESTRUCTIVE", "owner", None))
    assert used(budget) == 10


def test_a_read_is_allowed_and_charges_one_unit():
    budget, store, _ = make()
    assert allowed(budget.admit("list_dir", "files", Tier.READ, "owner", None))
    assert store.data[units_key()] == 1


# ------------------------------------------------------- weighted ceiling

def test_the_ceiling_allows_exactly_600_units_and_refuses_the_next_one():
    budget, store, _ = make()
    store.data[units_key()] = 597
    assert allowed(budget.admit("file_copy", "files", Tier.LOCAL_WRITE, "owner", None))
    assert used(budget) == DAILY_UNITS
    verdict = budget.admit("list_dir", "files", Tier.READ, "owner", None)
    assert verdict.decision is Decision.DENY
    assert verdict.code == "budget_exceeded"
    assert used(budget) == DAILY_UNITS


def test_a_denied_call_charges_nothing():
    budget, store, clock = make()
    for _ in range(30):
        budget.admit("list_dir", "files", Tier.READ, "owner", None)
    before = dict(store.data)
    verdict = budget.admit("list_dir", "files", Tier.READ, "owner", None)
    assert verdict.code == "rate_limited"
    assert store.data == before


# ------------------------------------------------------------ counted caps

@pytest.mark.parametrize("key, value, tool, family, tier, chat_id, code", [
    pytest.param("destructive", 30, "file_delete", "files", Tier.DESTRUCTIVE, None, "budget_exceeded",
                 id="destructive_cap"),
    pytest.param("system_change", 30, "volume_set", "system", Tier.SYSTEM_CHANGE, None, "budget_exceeded",
                 id="system_change_cap"),
    pytest.param("shell_free_text", 10, "shell_run", "shell", Tier.SYSTEM_CHANGE, None, "budget_exceeded",
                 id="free_text_shell_cap"),
    pytest.param("outbound", 20, "control_app_send", "desktop_control", Tier.OUTBOUND, None, "budget_exceeded",
                 id="outbound_cap_for_non_owner_sends"),
    pytest.param("llm_rounds", 1500, "llm_round", "llm", Tier.READ, None, "budget_exceeded",
                 id="llm_round_cap"),
])
def test_counted_caps_refuse_the_call_past_the_limit(key, value, tool, family, tier, chat_id, code):
    budget, store, _ = make()
    store.data[f"budget:{DAY}:{key}"] = value
    verdict = budget.admit(tool, family, tier, "owner", chat_id)
    assert verdict.decision is Decision.DENY
    assert verdict.code == code


def test_readonly_shell_is_not_limited_by_the_free_text_shell_cap():
    budget, store, _ = make()
    store.data[f"budget:{DAY}:shell_free_text"] = 10
    assert allowed(budget.admit("shell_readonly", "shell", Tier.READ, "owner", None))


def test_sends_to_the_owner_chat_do_not_count_toward_the_outbound_cap():
    budget, store, _ = make()
    store.data["owner_chat_id"] = 111
    store.data[f"budget:{DAY}:outbound"] = 20
    assert allowed(budget.admit("notify", "telegram", Tier.OUTBOUND, "owner", 111))
    assert store.data[f"budget:{DAY}:outbound"] == 20
    other = budget.admit("notify", "telegram", Tier.OUTBOUND, "owner", 222)
    assert other.code == "budget_exceeded"


def test_a_destructive_action_counts_once_toward_its_cap():
    budget, store, _ = make()
    store.data[f"budget:{DAY}:destructive"] = 29
    assert allowed(budget.admit("file_delete", "files", Tier.DESTRUCTIVE, "owner", None))
    assert store.data[f"budget:{DAY}:destructive"] == 30


# ------------------------------------------------------------ rate buckets

def test_reads_are_limited_to_thirty_a_minute_and_refill_after_a_minute():
    budget, _, clock = make()
    for _ in range(30):
        assert allowed(budget.admit("list_dir", "files", Tier.READ, "owner", None))
    verdict = budget.admit("list_dir", "files", Tier.READ, "owner", None)
    assert verdict.decision is Decision.DENY and verdict.code == "rate_limited"
    clock.advance(59.5)
    assert budget.admit("list_dir", "files", Tier.READ, "owner", None).code == "rate_limited"
    clock.advance(0.5)
    assert allowed(budget.admit("list_dir", "files", Tier.READ, "owner", None))


def test_local_writes_are_limited_to_ten_a_minute():
    budget, _, _ = make()
    for _ in range(10):
        assert allowed(budget.admit("note_add", "notes", Tier.LOCAL_WRITE, "owner", None))
    assert budget.admit("note_add", "notes", Tier.LOCAL_WRITE, "owner", None).code == "rate_limited"


def test_telegram_sends_have_their_own_bucket_for_each_chat():
    budget, store, _ = make()
    store.data["owner_chat_id"] = 111
    for _ in range(20):
        assert allowed(budget.admit("notify", "telegram", Tier.OUTBOUND, "owner", 111))
    assert budget.admit("notify", "telegram", Tier.OUTBOUND, "owner", 111).code == "rate_limited"
    assert allowed(budget.admit("notify", "telegram", Tier.OUTBOUND, "owner", 222))


def test_non_telegram_outbound_is_limited_to_six_a_minute():
    budget, _, _ = make()
    for _ in range(6):
        assert allowed(budget.admit("control_app_send", "desktop_control", Tier.OUTBOUND, "owner", None))
    assert budget.admit("control_app_send", "desktop_control", Tier.OUTBOUND, "owner", None).code == "rate_limited"


def test_llm_rounds_have_their_own_rate_and_do_not_use_the_tool_buckets():
    budget, _, _ = make()
    for _ in range(30):
        assert allowed(budget.admit("llm_round", "llm", Tier.READ, "scheduler", None))
    assert budget.admit("llm_round", "llm", Tier.READ, "scheduler", None).code == "rate_limited"
    assert allowed(budget.admit("list_dir", "files", Tier.READ, "owner", None))


def test_concurrent_admission_never_exceeds_the_bucket():
    budget, _, _ = make()
    results: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        verdict = budget.admit("list_dir", "files", Tier.READ, "owner", None)
        with lock:
            results.append(allowed(verdict))

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 30


# ----------------------------------------------------------------- day rollover

def test_the_counters_restart_at_local_midnight():
    budget, store, clock = make(DAY, hour=23, minute=59)
    store.data[units_key(DAY)] = DAILY_UNITS
    assert budget.admit("list_dir", "files", Tier.READ, "owner", None).code == "budget_exceeded"
    clock.set(at(NEXT_DAY, hour=0, minute=1))
    assert allowed(budget.admit("list_dir", "files", Tier.READ, "owner", None))
    assert budget.usage()["day"] == NEXT_DAY
    assert budget.usage()["units"]["used"] == 1


def test_day_counters_are_kept_under_budget_keys_in_the_store():
    budget, store, _ = make()
    budget.admit("file_delete", "files", Tier.DESTRUCTIVE, "owner", None)
    assert store.data[f"budget:{DAY}:units"] == 10
    assert store.data[f"budget:{DAY}:destructive"] == 1


# ------------------------------------------------------------------ bytes

def test_outgoing_bytes_are_capped_per_chat_per_day():
    budget, _, _ = make()
    assert allowed(budget.admit_bytes(BYTES_PER_DAY, 111))
    assert budget.admit_bytes(1, 111).code == "budget_exceeded"
    assert allowed(budget.admit_bytes(1, 222))


def test_bytes_cap_resets_the_next_day():
    budget, _, clock = make(DAY)
    assert allowed(budget.admit_bytes(BYTES_PER_DAY, 111))
    clock.set(at(NEXT_DAY))
    assert allowed(budget.admit_bytes(BYTES_PER_DAY, 111))


def test_negative_byte_counts_are_refused():
    budget, _, _ = make()
    verdict = budget.admit_bytes(-1, 111)
    assert verdict.decision is Decision.DENY
    assert verdict.code == "arg_invalid"


# --------------------------------------------------------------- warning

def test_warning_is_due_once_a_day_at_eighty_percent():
    budget, store, _ = make()
    store.data[units_key()] = 479
    assert budget.warning_due() is False
    budget.admit("list_dir", "files", Tier.READ, "owner", None)
    assert used(budget) == 480
    assert budget.warning_due() is True
    assert budget.warning_due() is False


def test_warning_is_due_again_on_the_next_day():
    budget, store, clock = make(DAY)
    store.data[units_key(DAY)] = 480
    assert budget.warning_due() is True
    clock.set(at(NEXT_DAY))
    store.data[units_key(NEXT_DAY)] = 480
    assert budget.warning_due() is True


def test_no_warning_below_eighty_percent():
    budget, store, _ = make()
    store.data[units_key()] = 479
    assert budget.warning_due() is False


# ----------------------------------------------------------------- usage

def test_usage_reports_units_counts_and_the_warning_flag():
    budget, _, _ = make()
    budget.admit("file_delete", "files", Tier.DESTRUCTIVE, "owner", None)
    report = budget.usage()
    assert report["day"] == DAY
    assert report["units"] == {"used": 10, "ceiling": DAILY_UNITS}
    assert report["counts"]["destructive"] == {"used": 1, "cap": 30}
    assert report["counts"]["llm_rounds"] == {"used": 0, "cap": 1500}
    assert report["warned"] is False
