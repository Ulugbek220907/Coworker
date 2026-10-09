"""PollLoop: offset ordering, restart, 409, backoff, idempotency and pairing.

The loop runs against FakeBotApi, which serves queued updates with Telegram's
offset rule, and against a real OwnerGate built on a FakePairing. The stop
event is a fake: backoff pauses are recorded, never slept, and the loop ends
when the fake Telegram runs dry.
"""
from __future__ import annotations

import logging

from coworker.transport.gate import OwnerGate
from coworker.transport.poll import CONFLICT_WINDOW_S, DROP_WINDOW_S, LAST_HANDLED_KEY, OFFSET_KEY, PollLoop
from fakes.telegram_fake import (
    PAIRED_REPLY, REFUSED_REPLY, FakeBotApi, FakePairing, FakeStopEvent, FakeStore,
)

OWNER_USER = 111
OWNER_CHAT = 111  # a private chat id equals the user id
NETWORK_DOWN = {"ok": False, "error_code": None, "description": "network error"}
CONFLICT = {"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates request"}


class FakeClock:
    """Monotonic time that moves only when a test says so, or when the loop sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def msg(update_id: int, *, user: int = OWNER_USER, chat: int = OWNER_CHAT,
        chat_type: str = "private", text: str = "salom") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": user},
            "chat": {"id": chat, "type": chat_type},
            "text": text,
        },
    }


def make_loop(api: FakeBotApi, store: FakeStore, handle, *, owner=(OWNER_USER, OWNER_CHAT), redeem_ok=False,
              clock: FakeClock | None = None, on_stopped=None) -> PollLoop:
    gate = OwnerGate(store, pairing=FakePairing(owner=owner, redeem_ok=redeem_ok))
    return PollLoop(api, store, gate, handle, on_stopped=on_stopped, clock=clock or FakeClock())


def drive(api: FakeBotApi, loop: PollLoop, stop: FakeStopEvent | None = None) -> FakeStopEvent:
    """Run the loop until the fake Telegram has nothing left to serve."""
    stop = stop or FakeStopEvent()
    api.stop_when_idle = stop
    loop.run(stop)
    return stop


def ids(updates: list[dict]) -> list[int]:
    return [u["update_id"] for u in updates]


def get_offsets(api: FakeBotApi) -> list[int]:
    return [kwargs["offset"] for name, kwargs in api.calls if name == "get_updates"]


# ----------------------------------------------------------------- offsets


def test_the_offset_is_persisted_before_the_batch_is_handled():
    store = FakeStore()
    api = FakeBotApi()
    api.queued_updates = [msg(10), msg(11)]
    order: list[tuple] = []
    real_set = store.kv_set

    def traced_set(key, value):
        order.append(("kv", key, value))
        real_set(key, value)

    store.kv_set = traced_set
    drive(api, make_loop(api, store, lambda u: order.append(("handled", u["update_id"]))))

    assert order.index(("kv", OFFSET_KEY, 12)) < order.index(("handled", 10))


def test_a_restart_does_not_replay_confirmed_updates():
    store = FakeStore()
    api = FakeBotApi()
    api.queued_updates = [msg(1), msg(2)]
    first: list[dict] = []
    drive(api, make_loop(api, store, first.append))
    assert ids(first) == [1, 2]

    api.queued_updates.append(msg(3))
    api.calls.clear()
    second: list[dict] = []
    drive(api, make_loop(api, store, second.append))

    assert ids(second) == [3]
    assert get_offsets(api)[0] == 3
    assert store.kv[OFFSET_KEY] == 4


def test_the_first_request_after_a_restart_starts_at_the_stored_offset():
    store = FakeStore()
    store.kv[OFFSET_KEY] = 57
    api = FakeBotApi()

    drive(api, make_loop(api, store, lambda u: None))

    assert get_offsets(api)[0] == 57


def test_a_redelivered_update_is_handled_once():
    store = FakeStore()
    api = FakeBotApi()
    api.poll_script = [
        {"ok": True, "result": [msg(5)]},
        {"ok": True, "result": [msg(5)]},  # the same id again, ignoring the offset
    ]
    handled: list[dict] = []

    drive(api, make_loop(api, store, handled.append))

    assert ids(handled) == [5]


def test_the_handled_marker_moves_forward_with_each_update():
    store = FakeStore()
    api = FakeBotApi()
    api.queued_updates = [msg(7), msg(8)]

    drive(api, make_loop(api, store, lambda u: None))

    assert store.kv[LAST_HANDLED_KEY] == 8


def test_a_malformed_update_without_an_id_is_skipped():
    store = FakeStore()
    api = FakeBotApi()
    api.poll_script = [{"ok": True, "result": [{"message": {"text": "no id"}}, msg(9)]}]
    handled: list[dict] = []

    drive(api, make_loop(api, store, handled.append))

    assert ids(handled) == [9]


# ------------------------------------------------------------ 409 and backoff


def test_a_409_right_after_a_restart_is_retried_until_the_old_poll_ends():
    """The previous process's long poll can still be open for up to LONG_POLL_S after a restart."""
    store = FakeStore()
    api = FakeBotApi()
    api.poll_script = [CONFLICT, CONFLICT]
    api.queued_updates = [msg(90, text="/status")]
    clock = FakeClock()
    stop = FakeStopEvent(on_wait=clock.sleep)
    handled: list[dict] = []
    offline: list[str] = []

    drive(api, make_loop(api, store, handled.append, clock=clock, on_stopped=offline.append), stop)

    assert ids(handled) == [90]
    assert stop.waits == [1, 2]
    assert offline == []


def test_a_409_that_outlasts_the_window_stops_polling_reports_offline_and_logs_loudly(caplog):
    store = FakeStore()
    api = FakeBotApi()
    clock = FakeClock()
    stop = FakeStopEvent(on_wait=clock.sleep)
    offline: list[str] = []

    def always_conflict(offset, timeout=25):
        api.calls.append(("get_updates", {"offset": offset, "timeout": timeout}))
        return CONFLICT

    api.get_updates = always_conflict
    loop = make_loop(api, store, lambda u: None, clock=clock, on_stopped=offline.append)

    with caplog.at_level(logging.CRITICAL, logger="transport.poll"):
        loop.run(stop)

    assert clock.now >= CONFLICT_WINDOW_S
    assert stop.waits == [1, 2, 4, 8, 16, 32, 60]
    assert api.methods().count("get_updates") == 8
    assert len(offline) == 1 and "409" in offline[0]
    assert any(r.levelno == logging.CRITICAL and "409" in r.getMessage() for r in caplog.records)


def test_a_success_between_conflicts_starts_a_new_window():
    store = FakeStore()
    api = FakeBotApi()
    # Six conflicts cover 63 s, one success resets the window, six more conflicts. Without the
    # reset, the second streak would start 63 s into a window that closes at 90 s and stop early.
    api.poll_script = [CONFLICT] * 6 + [{"ok": True, "result": []}] + [CONFLICT] * 6
    api.queued_updates = [msg(95, text="/status")]
    clock = FakeClock()
    stop = FakeStopEvent(on_wait=clock.sleep)
    handled: list[dict] = []
    offline: list[str] = []

    drive(api, make_loop(api, store, handled.append, clock=clock, on_stopped=offline.append), stop)

    assert ids(handled) == [95]
    assert stop.waits == [1, 2, 4, 8, 16, 32] * 2
    assert offline == []


def test_network_errors_between_conflicts_do_not_restart_the_window():
    store = FakeStore()
    api = FakeBotApi()
    clock = FakeClock()
    # The stop event is bounded so that a regression shows up as a failed assertion, not a hang.
    stop = FakeStopEvent(on_wait=clock.sleep, stop_after_waits=60)
    offline: list[str] = []
    replies = [CONFLICT, NETWORK_DOWN]

    def alternating(offset, timeout=25):
        api.calls.append(("get_updates", {"offset": offset, "timeout": timeout}))
        return replies[len(api.calls) % 2]

    api.get_updates = alternating

    make_loop(api, store, lambda u: None, clock=clock, on_stopped=offline.append).run(stop)

    assert len(offline) == 1 and "409" in offline[0]
    assert clock.now >= CONFLICT_WINDOW_S


def test_a_normal_stop_does_not_report_offline_through_the_conflict_callback():
    store = FakeStore()
    api = FakeBotApi()
    offline: list[str] = []

    drive(api, make_loop(api, store, lambda u: None, on_stopped=offline.append))

    assert offline == []


def test_the_backoff_doubles_from_one_second_to_a_sixty_second_ceiling():
    store = FakeStore()
    api = FakeBotApi()
    api.poll_script = [NETWORK_DOWN] * 8

    stop = drive(api, make_loop(api, store, lambda u: None))

    assert stop.waits == [1, 2, 4, 8, 16, 32, 60, 60]


def test_a_success_resets_the_backoff_to_one_second():
    store = FakeStore()
    api = FakeBotApi()
    api.poll_script = [NETWORK_DOWN, NETWORK_DOWN, {"ok": True, "result": []}, NETWORK_DOWN]

    stop = drive(api, make_loop(api, store, lambda u: None))

    assert stop.waits == [1, 2, 1]


def test_a_stop_during_backoff_ends_the_loop_at_once():
    store = FakeStore()
    api = FakeBotApi()
    api.poll_script = [NETWORK_DOWN] * 3
    stop = FakeStopEvent(stop_after_waits=1)

    drive(api, make_loop(api, store, lambda u: None), stop)

    assert stop.waits == [1]
    assert api.methods().count("get_updates") == 1


def test_the_webhook_is_deleted_once_at_start_not_on_every_cycle():
    store = FakeStore()
    api = FakeBotApi()
    api.poll_script = [NETWORK_DOWN] * 3 + [{"ok": True, "result": []}]

    drive(api, make_loop(api, store, lambda u: None))

    assert api.methods().count("delete_webhook") == 1
    assert api.methods().count("get_updates") >= 4


def test_an_unexpected_exception_in_a_cycle_backs_off_and_continues():
    store = FakeStore()
    api = FakeBotApi()
    api.queued_updates = [msg(20)]
    real_get = api.get_updates
    attempts: list[int] = []

    def flaky_get(offset, timeout=25):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("an unexpected library error")
        return real_get(offset, timeout)

    api.get_updates = flaky_get
    handled: list[dict] = []

    stop = drive(api, make_loop(api, store, handled.append))

    assert stop.waits[0] == 1
    assert ids(handled) == [20]


# -------------------------------------------------------- gate and handler


def test_a_handler_failure_does_not_stall_the_queue_behind_it(caplog):
    store = FakeStore()
    api = FakeBotApi()
    api.queued_updates = [msg(30), msg(31)]
    handled: list[int] = []

    def handle(update):
        if update["update_id"] == 30:
            raise ValueError("bad parse")
        handled.append(update["update_id"])

    with caplog.at_level(logging.ERROR, logger="transport.poll"):
        drive(api, make_loop(api, store, handle))

    assert handled == [31]
    assert store.kv[LAST_HANDLED_KEY] == 31
    assert any("failed in the handler" in r.getMessage() for r in caplog.records)


def test_a_non_owner_update_is_dropped_and_audited_but_never_handled():
    store = FakeStore()
    api = FakeBotApi()
    api.queued_updates = [msg(40, user=999, chat=999)]
    handled: list[dict] = []

    drive(api, make_loop(api, store, handled.append))

    assert handled == []
    assert len(store.audit_rows) == 1
    assert store.audit_rows[0]["code"] == "gate_dropped"
    assert store.audit_rows[0]["args_summary"] == "kind=message chat=private"
    assert store.outcomes == [{"intent_id": 1, "ok": False, "code": "gate_dropped", "summary": "dropped before parsing"}]


def test_the_audit_row_never_carries_the_sender_or_the_text():
    store = FakeStore()
    api = FakeBotApi()
    api.queued_updates = [msg(41, user=999, chat=999, chat_type="group", text="secret words")]

    drive(api, make_loop(api, store, lambda u: None))

    summary = store.audit_rows[0]["args_summary"]
    assert "999" not in summary
    assert "secret" not in summary
    assert summary == "kind=message chat=group"


def test_a_callback_from_a_stranger_is_dropped_and_audited():
    store = FakeStore()
    api = FakeBotApi()
    api.queued_updates = [{
        "update_id": 42,
        "callback_query": {
            "id": "cb", "from": {"id": 999}, "data": "opt:0",
            "message": {"message_id": 3, "chat": {"id": 999, "type": "private"}},
        },
    }]
    handled: list[dict] = []

    drive(api, make_loop(api, store, handled.append))

    assert handled == []
    assert store.audit_rows[0]["args_summary"] == "kind=callback_query chat=private"


def test_an_owner_update_reaches_the_handler_without_an_audit_row():
    store = FakeStore()
    api = FakeBotApi()
    api.queued_updates = [msg(50, text="/status")]
    handled: list[dict] = []

    drive(api, make_loop(api, store, handled.append))

    assert ids(handled) == [50]
    assert store.audit_rows == []


def test_before_pairing_a_connect_is_redeemed_and_answered_with_success():
    store = FakeStore()
    api = FakeBotApi()
    api.queued_updates = [msg(60, text="/connect 123456")]
    handled: list[dict] = []

    drive(api, make_loop(api, store, handled.append, owner=None, redeem_ok=True))

    assert handled == []
    sent = [kwargs for name, kwargs in api.calls if name == "send_message"]
    assert sent == [{"chat_id": OWNER_CHAT, "text": PAIRED_REPLY, "buttons": None, "reply_to": None}]


def test_before_pairing_a_refused_code_is_answered_with_the_refusal():
    store = FakeStore()
    api = FakeBotApi()
    api.queued_updates = [msg(61, text="/connect 000000")]

    drive(api, make_loop(api, store, lambda u: None, owner=None, redeem_ok=False))

    sent = [kwargs for name, kwargs in api.calls if name == "send_message"]
    assert sent[0]["text"] == REFUSED_REPLY


def test_the_reply_is_the_pairing_own_text_so_a_lockout_is_not_called_a_wrong_code():
    store = FakeStore()
    api = FakeBotApi()
    api.queued_updates = [msg(64, text="/connect 999999")]
    lockout = "Juda ko'p noto'g'ri urinish bo'ldi. Bir soatdan keyin qayta urinib ko'ring."
    pairing = FakePairing(owner=None, redeem_ok=False, refusal=lockout)
    gate = OwnerGate(store, pairing=pairing)

    drive(api, PollLoop(api, store, gate, lambda u: None))

    sent = [kwargs for name, kwargs in api.calls if name == "send_message"]
    assert sent[0]["text"] == lockout


def test_before_pairing_other_messages_are_dropped_without_a_reply():
    store = FakeStore()
    api = FakeBotApi()
    api.queued_updates = [msg(62, text="/start"), msg(63, text="salom")]
    handled: list[dict] = []

    drive(api, make_loop(api, store, handled.append, owner=None))

    assert handled == []
    assert "send_message" not in api.methods()
    assert [row["code"] for row in store.audit_rows] == ["gate_dropped", "gate_dropped"]


def test_a_gate_that_raises_fails_closed_and_the_update_is_dropped():
    store = FakeStore()
    api = FakeBotApi()
    api.queued_updates = [msg(70)]
    handled: list[dict] = []

    class BrokenGate:
        def allows(self, update):
            raise RuntimeError("gate broke")

        def paired(self):
            return True

    drive(api, PollLoop(api, store, BrokenGate(), handled.append))

    assert handled == []
    assert store.audit_rows[0]["code"] == "gate_dropped"


def test_an_update_that_cannot_be_recorded_does_not_block_the_owners_next_one(caplog):
    class FailingAuditStore(FakeStore):
        def audit_intent(self, **kwargs):
            raise RuntimeError("audit key unavailable")

    store = FailingAuditStore()
    api = FakeBotApi()
    api.queued_updates = [msg(80, user=999, chat=999), msg(81, text="/stop")]
    handled: list[dict] = []

    with caplog.at_level(logging.ERROR, logger="transport.poll"):
        drive(api, make_loop(api, store, handled.append))

    assert ids(handled) == [81]
    assert store.kv[LAST_HANDLED_KEY] == 81
    assert any("could not be processed" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------- stranger floods


def test_a_stranger_flood_writes_two_audit_rows_not_one_per_update():
    """Telegram delivers as fast as a stranger sends; the audit table must not grow with that."""
    store = FakeStore()
    api = FakeBotApi()
    api.queued_updates = [msg(200 + i, user=999, chat=999) for i in range(500)]

    drive(api, make_loop(api, store, lambda u: None))

    assert [row["args_summary"] for row in store.audit_rows] == [
        "kind=message chat=private",
        "kind=message chat=private repeats=499",
    ]


def test_drops_are_counted_per_window_and_a_new_window_starts_a_new_row():
    store = FakeStore()
    api = FakeBotApi()
    clock = FakeClock()
    api.queued_updates = [msg(300, user=999, chat=999), msg(301, user=999, chat=999)]
    loop = make_loop(api, store, lambda u: None, clock=clock)
    drive(api, loop)

    clock.now += DROP_WINDOW_S
    api.queued_updates = [msg(302, user=999, chat=999)]
    drive(api, loop)

    assert [row["args_summary"] for row in store.audit_rows] == [
        "kind=message chat=private",
        "kind=message chat=private repeats=1",
        "kind=message chat=private",
    ]


def test_a_stranger_message_and_a_stranger_callback_are_counted_separately():
    store = FakeStore()
    api = FakeBotApi()
    api.queued_updates = [
        msg(400, user=999, chat=999),
        {
            "update_id": 401,
            "callback_query": {
                "id": "cb", "from": {"id": 999}, "data": "opt:0",
                "message": {"message_id": 3, "chat": {"id": 999, "type": "private"}},
            },
        },
    ]

    drive(api, make_loop(api, store, lambda u: None))

    assert [row["args_summary"] for row in store.audit_rows] == [
        "kind=message chat=private",
        "kind=callback_query chat=private",
    ]
