"""Governor: pressure pauses with hysteresis, refusal codes, pools, abandon and cancel."""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable

import pytest

from coworker.core.types import CancelToken, Cancelled
from coworker.governor import GovClass, GovTimeout, Governor, Limits, Refused
from tests.fakes import FakeOs


class Clock:
    """A fake monotonic clock for the pressure windows and clear timers."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def stuck(release: threading.Event) -> Callable[[], str]:
    """A job that returns only after the test lets it go."""

    def job() -> str:
        release.wait(5)
        return "late"

    return job


def reasons(gov: Governor) -> list[str]:
    return gov.pressure()["reasons"]


# ------------------------------------------------------------ pressure gates

CONDITIONS = [
    pytest.param({"idle_s": 1.0}, "owner_active", {"idle_s": 600.0}, id="owner_active"),
    pytest.param({"saver": True}, "battery_saver", {"saver": False}, id="battery_saver"),
    pytest.param({"ram_mb": 1000.0}, "low_ram", {"ram_mb": 4000.0}, id="low_ram"),
    pytest.param({"cpu_pct": 85.0}, "cpu_high", {"cpu_pct": 10.0}, id="cpu_high"),
    pytest.param({"plugged": False, "battery_pct": 20}, "battery_low", {"plugged": True}, id="battery_low"),
    pytest.param({"cpu_pct": 99.0}, "cpu_critical", {"cpu_pct": 10.0}, id="cpu_critical"),
]


@pytest.mark.parametrize("bad, reason, good", CONDITIONS)
def test_each_condition_pauses_at_once_and_resumes_only_after_thirty_seconds_clear(bad, reason, good):
    os = FakeOs(**bad)
    clock = Clock()
    gov = Governor(os, monotonic=clock)
    assert reason in reasons(gov)

    for name, value in good.items():
        setattr(os, name, value)
    assert reason in reasons(gov)          # the clear period has only just begun
    clock.advance(29.5)
    assert reason in reasons(gov)          # still inside the 30 s clear period
    clock.advance(0.5)
    assert reason not in reasons(gov)      # clear for 30 s: resumes


@pytest.mark.parametrize("kwargs, not_tripped", [
    pytest.param({"idle_s": 5.0}, "owner_active", id="owner_idle_at_five_seconds"),
    pytest.param({"ram_mb": 1536.0}, "low_ram", id="ram_at_1_5_gb"),
    pytest.param({"cpu_pct": 80.0}, "cpu_high", id="cpu_at_80"),
    pytest.param({"plugged": False, "battery_pct": 25}, "battery_low", id="battery_at_25"),
    pytest.param({"cpu_pct": 95.0}, "cpu_critical", id="cpu_at_95"),
    pytest.param({"plugged": False, "battery_pct": None}, "battery_low", id="no_battery_reading"),
])
def test_thresholds_are_strict_at_the_boundary(kwargs, not_tripped):
    gov = Governor(FakeOs(**kwargs), monotonic=Clock())
    assert not_tripped not in reasons(gov)


def test_flapping_condition_restarts_the_clear_timer():
    os = FakeOs(idle_s=1.0)
    clock = Clock()                        # t=1000: the owner is at the keyboard
    gov = Governor(os, monotonic=clock)
    assert reasons(gov) == ["owner_active"]
    os.idle_s = 600.0
    clock.advance(20)                      # t=1020: the clear period starts
    assert reasons(gov) == ["owner_active"]
    clock.advance(10)
    os.idle_s = 1.0                        # t=1030: the owner is back and the timer restarts
    assert reasons(gov) == ["owner_active"]
    os.idle_s = 600.0
    clock.advance(5)                       # t=1035: a new clear period starts
    assert reasons(gov) == ["owner_active"]
    clock.advance(29.5)                    # t=1064.5: 29.5 s clear, which is not enough
    assert "owner_active" in reasons(gov)
    clock.advance(0.5)                     # t=1065: 30 s clear since t=1035
    assert "owner_active" not in reasons(gov)


def test_one_cpu_spike_is_averaged_and_pauses_nothing():
    os = FakeOs(cpu_pct=60.0)
    clock = Clock()
    gov = Governor(os, monotonic=clock)
    gov.pressure()
    os.cpu_pct = 100.0
    clock.advance(1)
    state = gov.pressure()
    assert state["cpu_mean_pct"] == 80.0   # mean of 60 and 100 is exactly the threshold
    assert "cpu_high" not in state["reasons"]


def test_cpu_window_forgets_samples_older_than_ten_seconds():
    os = FakeOs(cpu_pct=90.0)
    clock = Clock()
    gov = Governor(os, monotonic=clock)
    assert "cpu_high" in reasons(gov)
    os.cpu_pct = 10.0
    clock.advance(11)
    assert gov.pressure()["cpu_mean_pct"] == 10.0


def test_pressure_view_reports_the_signals_it_was_given():
    os = FakeOs(idle_s=12.4, cpu_pct=33.3, ram_mb=2048.7, battery_pct=77, plugged=True, saver=False)
    view = Governor(os, monotonic=Clock()).pressure()
    assert view["idle_s"] == 12.4
    assert view["cpu_pct"] == 33.3
    assert view["free_ram_mb"] == 2049
    assert view["battery_pct"] == 77
    assert view["plugged"] is True
    assert view["battery_saver"] is False
    assert view["input_desktop"] is True


# ------------------------------------------------------------ refusal codes

def test_owner_turns_run_even_when_the_cpu_is_critical():
    gov = Governor(FakeOs(cpu_pct=99.0), monotonic=Clock())
    assert asyncio.run(gov.run("TOOL", lambda: "ok", timeout_s=5, interactive=True)) == "ok"


def test_unattended_calls_are_refused_with_paused_when_the_cpu_is_critical():
    ran: list[str] = []
    gov = Governor(FakeOs(cpu_pct=99.0), monotonic=Clock())

    async def main():
        await gov.run("TOOL", lambda: ran.append("x"), timeout_s=5, interactive=False)

    with pytest.raises(Refused) as info:
        asyncio.run(main())
    assert info.value.code == "paused"
    assert ran == []                       # nothing ran: the refusal happens before the job


def test_background_classes_pause_under_low_ram_but_ordinary_pools_do_not():
    gov = Governor(FakeOs(ram_mb=1000.0), monotonic=Clock())

    async def background():
        await gov.run("INDEX", lambda: "x", timeout_s=5, interactive=False)

    with pytest.raises(Refused) as info:
        asyncio.run(background())
    assert info.value.code == "paused"
    assert asyncio.run(gov.run("TOOL", lambda: "ok", timeout_s=5, interactive=False)) == "ok"


def test_unattended_calls_are_refused_with_low_memory_under_800_mb():
    gov = Governor(FakeOs(ram_mb=700.0), monotonic=Clock())

    async def main():
        await gov.run("TOOL", lambda: "x", timeout_s=5, interactive=False)

    with pytest.raises(Refused) as info:
        asyncio.run(main())
    assert info.value.code == "low_memory"


def test_owner_turns_are_not_refused_for_low_memory():
    gov = Governor(FakeOs(ram_mb=700.0), monotonic=Clock())
    assert asyncio.run(gov.run("INDEX", lambda: "ok", timeout_s=5, interactive=True)) == "ok"


def test_locked_desktop_refuses_input_even_for_the_owner():
    gov = Governor(FakeOs(input_desktop=False), monotonic=Clock())

    async def main():
        await gov.run("INPUT", lambda: "typed", timeout_s=5, interactive=True)

    with pytest.raises(Refused) as info:
        asyncio.run(main())
    assert info.value.code == "locked_desktop"
    assert asyncio.run(gov.run("TOOL", lambda: "ok", timeout_s=5)) == "ok"


def test_background_work_resumes_only_after_the_clear_period_in_run():
    os = FakeOs(idle_s=1.0)
    clock = Clock()
    gov = Governor(os, monotonic=clock)

    async def index_job():
        return await gov.run("INDEX", lambda: "indexed", timeout_s=5, interactive=False)

    with pytest.raises(Refused) as info:
        asyncio.run(index_job())
    assert info.value.code == "paused"
    os.idle_s = 600.0
    with pytest.raises(Refused):
        asyncio.run(index_job())           # the clear period has only just started
    clock.advance(30)
    assert asyncio.run(index_job()) == "indexed"


def test_refusal_and_timeout_errors_carry_a_code_attribute():
    assert Refused("paused").code == "paused"
    timeout = GovTimeout()
    assert timeout.code == "timeout"
    assert timeout.abandoned is False


# ----------------------------------------------------------------- pools

def test_default_pool_sizes_match_section_seven():
    gov = Governor(FakeOs(), monotonic=Clock())
    limits = {name: pool["limit"] for name, pool in gov.status()["pools"].items()}
    assert limits == {
        "TOOL": 4, "NET": 4, "LLM": 2, "UIA": 1, "INPUT": 1, "VISION": 1,
        "BROWSER": 1, "OFFICE": 1, "SHELL": 1, "INDEX": 1, "STT": 1,
    }


def test_limits_override_single_pools():
    gov = Governor(FakeOs(), limits=Limits(pool_sizes={GovClass.TOOL: 1}), monotonic=Clock())
    assert gov.status()["pools"]["TOOL"]["limit"] == 1
    assert gov.status()["pools"]["NET"]["limit"] == 4


def test_none_class_runs_in_the_tool_pool():
    gov = Governor(FakeOs(), monotonic=Clock())
    assert asyncio.run(gov.run("NONE", lambda: 7, timeout_s=5)) == 7
    assert "NONE" not in gov.status()["pools"]


def test_the_tool_pool_never_runs_more_than_four_jobs_at_once():
    gov = Governor(FakeOs(), monotonic=Clock())
    lock = threading.Lock()
    active = 0
    peak = 0

    def job() -> bool:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.1)
        with lock:
            active -= 1
        return True

    async def main():
        return await asyncio.gather(*(gov.run("TOOL", job, timeout_s=10) for _ in range(8)))

    assert asyncio.run(main()) == [True] * 8
    assert peak == 4


def test_waiter_cap_refuses_the_ninth_caller_at_once():
    gov = Governor(FakeOs(), monotonic=Clock())
    release = threading.Event()

    async def main():
        holder = asyncio.create_task(gov.run("OFFICE", stuck(release), timeout_s=10))
        await asyncio.sleep(0.1)           # the holder takes the only OFFICE slot
        waiters = [asyncio.create_task(gov.run("OFFICE", lambda: "later", timeout_s=10)) for _ in range(8)]
        await asyncio.sleep(0.2)
        assert gov.status()["pools"]["OFFICE"]["waiting"] == 8
        started = time.monotonic()
        with pytest.raises(Refused) as info:
            await gov.run("OFFICE", lambda: "extra", timeout_s=10)
        assert info.value.code == "throttled"
        assert time.monotonic() - started < 0.5   # fails fast; does not queue
        release.set()
        return await asyncio.gather(holder, *waiters)

    results = asyncio.run(main())
    assert results[0] == "late"
    assert results[1:] == ["later"] * 8


def test_the_slot_is_released_before_the_caller_resumes():
    gov = Governor(FakeOs(), monotonic=Clock())
    asyncio.run(gov.run("UIA", lambda: "ok", timeout_s=5))
    assert gov.status()["pools"]["UIA"]["running"] == 0


def test_a_function_exception_reaches_the_caller_unchanged():
    gov = Governor(FakeOs(), monotonic=Clock())

    def broken() -> None:
        raise ValueError("bad input")

    with pytest.raises(ValueError, match="bad input"):
        asyncio.run(gov.run("TOOL", broken, timeout_s=5))


def test_invalid_arguments_are_rejected_before_anything_runs():
    gov = Governor(FakeOs(), monotonic=Clock())
    with pytest.raises(ValueError):
        asyncio.run(gov.run("TOOL", lambda: 1, timeout_s=0))
    with pytest.raises(ValueError):
        asyncio.run(gov.run("NOPE", lambda: 1, timeout_s=5))


def test_a_job_that_outlasts_its_timeout_raises_timeout_for_an_ordinary_class():
    gov = Governor(FakeOs(), monotonic=Clock())
    release = threading.Event()

    async def main():
        with pytest.raises(GovTimeout) as info:
            await gov.run("TOOL", stuck(release), timeout_s=0.1)
        return info.value

    err = asyncio.run(main())
    assert err.code == "timeout"
    assert err.abandoned is False          # TOOL is not in the abandon rule
    release.set()
    assert wait_until(lambda: gov.status()["pools"]["TOOL"]["running"] == 0)


# --------------------------------------------------------- abandon rule

def test_timed_out_in_process_job_is_abandoned_and_its_slot_stays_taken():
    gov = Governor(FakeOs(), monotonic=Clock())
    release = threading.Event()

    async def main():
        with pytest.raises(GovTimeout) as info:
            await gov.run("UIA", stuck(release), timeout_s=0.1)
        return info.value

    err = asyncio.run(main())
    assert err.code == "timeout" and err.abandoned is True
    pool = gov.status()["pools"]["UIA"]
    assert pool["abandoned"] == 1
    assert pool["running"] == 1            # the thread is still inside the job
    release.set()
    assert wait_until(lambda: gov.status()["pools"]["UIA"]["running"] == 0)


def test_three_abandoned_jobs_disable_the_pool_until_restart():
    gov = Governor(FakeOs(), monotonic=Clock())
    for _ in range(3):
        release = threading.Event()

        async def abandoned_run(event=release):
            with pytest.raises(GovTimeout) as info:
                await gov.run("UIA", stuck(event), timeout_s=0.1)
            assert info.value.abandoned is True

        asyncio.run(abandoned_run())
        release.set()
        assert wait_until(lambda: gov.status()["pools"]["UIA"]["running"] == 0)

    pool = gov.status()["pools"]["UIA"]
    assert pool["abandoned"] == 3
    assert pool["disabled"] is True

    async def after_disable():
        await gov.run("UIA", lambda: "ok", timeout_s=5)

    with pytest.raises(Refused) as info:
        asyncio.run(after_disable())
    assert info.value.code == "throttled"


def test_a_job_that_never_started_is_not_counted_as_abandoned():
    gov = Governor(FakeOs(), monotonic=Clock())
    release = threading.Event()

    async def main():
        holder = asyncio.create_task(gov.run("UIA", stuck(release), timeout_s=10))
        await asyncio.sleep(0.1)
        with pytest.raises(GovTimeout) as info:
            await gov.run("UIA", lambda: "never", timeout_s=0.2)
        assert info.value.abandoned is False
        release.set()
        await holder

    asyncio.run(main())
    assert gov.status()["pools"]["UIA"]["abandoned"] == 0


# --------------------------------------------------------- cancellation

def test_cancel_token_stops_a_job_that_is_waiting_for_a_slot():
    gov = Governor(FakeOs(), monotonic=Clock())
    release = threading.Event()
    token = CancelToken()
    ran: list[str] = []

    async def main():
        holder = asyncio.create_task(gov.run("UIA", stuck(release), timeout_s=10))
        await asyncio.sleep(0.1)
        waiter = asyncio.create_task(
            gov.run("UIA", lambda: ran.append("ran"), timeout_s=10, cancel=token),
        )
        await asyncio.sleep(0.1)
        token.cancel()
        started = time.monotonic()
        with pytest.raises(Cancelled):
            await asyncio.wait_for(waiter, 2)
        assert time.monotonic() - started < 1.0
        release.set()
        await holder

    asyncio.run(main())
    assert ran == []
    assert gov.status()["pools"]["UIA"]["waiting"] == 0


def test_cancel_while_running_returns_at_once_and_is_not_an_abandon():
    gov = Governor(FakeOs(), monotonic=Clock())
    release = threading.Event()
    token = CancelToken()

    async def main():
        task = asyncio.create_task(gov.run("UIA", stuck(release), timeout_s=10, cancel=token))
        await asyncio.sleep(0.1)
        token.cancel()
        with pytest.raises(Cancelled):
            await asyncio.wait_for(task, 2)
        assert gov.status()["pools"]["UIA"]["abandoned"] == 0
        release.set()

    asyncio.run(main())
    assert wait_until(lambda: gov.status()["pools"]["UIA"]["running"] == 0)


def test_a_token_that_is_already_cancelled_runs_nothing():
    gov = Governor(FakeOs(), monotonic=Clock())
    token = CancelToken()
    token.cancel()
    ran: list[str] = []
    with pytest.raises(Cancelled):
        asyncio.run(gov.run("TOOL", lambda: ran.append("x"), timeout_s=5, cancel=token))
    assert ran == []
