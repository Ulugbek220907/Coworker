"""BotApi over an httpx.MockTransport, the token redaction filter, and the outbox.

The outbox tests live here because they cover the send path that BotApi
carries. No test opens a socket: the transport is a MockTransport and the
outbox receives a FakeBotApi.
"""
from __future__ import annotations

import json
import logging
import sys

import httpx
import pytest

from coworker.transport import bot as bot_mod
from coworker.transport import outbox as outbox_mod
from coworker.transport.bot import BotApi, TokenRedactor, install_redaction, redact
from coworker.transport.outbox import CHUNK, Outbox, option_rows, split_text
from fakes.telegram_fake import FakeBotApi, FakeStore

TOKEN = "123456789:AAFakeTokenForTestsOnly_0123456789abc"
OWNER_CHAT = 4242


def make_api(handler, sleeps: list[float] | None = None) -> BotApi:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return BotApi(TOKEN, client=client, sleep=(sleeps if sleeps is not None else []).append)


def ok_json(result=None) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result if result is not None else {}})


# ----------------------------------------------------------------- requests


def test_send_message_posts_json_to_the_method_url():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok_json({"message_id": 1})

    result = make_api(handler).send_message(OWNER_CHAT, "salom")

    assert result == {"ok": True, "result": {"message_id": 1}}
    assert seen[0].url.path == f"/bot{TOKEN}/sendMessage"
    body = json.loads(seen[0].content)
    assert body == {"chat_id": OWNER_CHAT, "text": "salom"}


def test_send_message_carries_the_keyboard_and_reply_to():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return ok_json()

    buttons = [[{"text": "Ha", "callback_data": "opt:0"}]]
    make_api(handler).send_message(OWNER_CHAT, "savol", buttons=buttons, reply_to=77)

    body = json.loads(seen[0].content)
    assert body["reply_markup"] == {"inline_keyboard": buttons}
    assert body["reply_to_message_id"] == 77
    assert body["allow_sending_without_reply"] is True


def test_get_updates_asks_for_messages_and_callbacks_from_the_offset():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return ok_json([])

    make_api(handler).get_updates(offset=10, timeout=25)

    body = json.loads(seen[0].content)
    assert body == {"offset": 10, "timeout": 25, "allowed_updates": ["message", "callback_query"]}


def test_edit_reply_markup_clears_the_keyboard():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return ok_json(True)

    make_api(handler).edit_reply_markup(OWNER_CHAT, 9)

    assert json.loads(seen[0].content)["reply_markup"] == {"inline_keyboard": []}


def test_send_document_uploads_the_file_with_a_trimmed_caption(tmp_path):
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-1.4 fake body")
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return ok_json({"message_id": 2})

    make_api(handler).send_document(OWNER_CHAT, str(report), caption="x" * 1500)

    raw = seen[0].content
    assert seen[0].url.path == f"/bot{TOKEN}/sendDocument"
    assert b'name="document"; filename="report.pdf"' in raw
    assert b"%PDF-1.4 fake body" in raw
    assert b"x" * 1000 in raw and b"x" * 1001 not in raw


def test_send_photo_uses_the_photo_field(tmp_path):
    picture = tmp_path / "scan.png"
    picture.write_bytes(b"\x89PNG fake")
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return ok_json()

    make_api(handler).send_photo(OWNER_CHAT, str(picture))

    assert seen[0].url.path == f"/bot{TOKEN}/sendPhoto"
    assert b'name="photo"; filename="scan.png"' in seen[0].content


def test_a_missing_upload_file_is_data_and_no_request_is_made(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return ok_json()

    result = make_api(handler).send_document(OWNER_CHAT, str(tmp_path / "gone.pdf"))

    assert result["ok"] is False
    assert seen == []


# ------------------------------------------------------------------ failures


def test_network_errors_are_retried_then_returned_as_data_without_raising():
    calls: list[int] = []
    sleeps: list[float] = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("name resolution failed")

    result = make_api(handler, sleeps).send_message(OWNER_CHAT, "hi")

    assert result == {"ok": False, "error_code": None, "description": "network error: name resolution failed"}
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


def test_a_read_timeout_counts_as_a_network_error():
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("read timed out")

    result = make_api(handler, []).get_updates(0)

    assert result["ok"] is False and result["error_code"] is None
    assert len(calls) == 3


def test_a_retry_that_succeeds_returns_the_success():
    attempts: list[int] = []
    sleeps: list[float] = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("reset")
        return ok_json({"message_id": 5})

    result = make_api(handler, sleeps).send_message(OWNER_CHAT, "hi")

    assert result["ok"] is True
    assert len(attempts) == 2
    assert sleeps == [1.0]


def test_an_http_error_answer_is_not_retried():
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        return httpx.Response(409, json={"ok": False, "error_code": 409, "description": "Conflict"})

    result = make_api(handler, []).get_updates(0)

    assert result["error_code"] == 409
    assert result["ok"] is False
    assert len(calls) == 1


def test_a_gateway_html_page_is_reported_by_status_not_by_body():
    def handler(request):
        return httpx.Response(502, text="<html>bad gateway " + "x" * 500 + "</html>")

    result = make_api(handler).send_message(OWNER_CHAT, "hi")

    assert result == {"ok": False, "error_code": 502, "description": "HTTP 502"}


def test_a_non_object_json_payload_is_a_failure():
    def handler(request):
        return httpx.Response(200, json=["not", "an", "object"])

    result = make_api(handler).send_message(OWNER_CHAT, "hi")

    assert result["ok"] is False


def test_get_file_bytes_downloads_within_the_size_limit():
    urls: list[str] = []

    def handler(request):
        urls.append(str(request.url))
        if request.url.path.endswith("/getFile"):
            return ok_json({"file_id": "F1", "file_path": "voice/1.oga", "file_size": 4})
        return httpx.Response(200, content=b"oggs")

    result = make_api(handler).get_file_bytes("F1", max_bytes=100)

    assert result == {"ok": True, "content": b"oggs"}
    assert urls[-1] == f"https://api.telegram.org/file/bot{TOKEN}/voice/1.oga"


def test_get_file_bytes_refuses_a_file_over_the_limit_without_downloading():
    urls: list[str] = []

    def handler(request):
        urls.append(request.url.path)
        return ok_json({"file_path": "big.bin", "file_size": 500})

    result = make_api(handler).get_file_bytes("F1", max_bytes=100)

    assert result["ok"] is False
    assert len(urls) == 1  # only getFile


# ----------------------------------------------------------------- redaction


def test_redact_removes_the_token_from_a_url_and_from_bare_text():
    url = f"https://api.telegram.org/bot{TOKEN}/getMe"
    assert "123456789" not in redact(url)
    assert "AAFakeToken" not in redact(url)
    assert redact(f"token was {TOKEN} once") == "token was <redacted> once"


def test_redact_leaves_ordinary_text_alone():
    text = "telegram sendMessage failed: Bad Request: chat not found (10:30)"
    assert redact(text) == text


def test_the_filter_rewrites_formatted_arguments_and_clears_them():
    record = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1,
        'HTTP Request: POST %s "HTTP/1.1 200 OK"',
        (httpx.URL(f"https://api.telegram.org/bot{TOKEN}/getUpdates"),), None,
    )

    assert TokenRedactor().filter(record) is True

    assert "AAFakeToken" not in record.getMessage()
    assert record.args == ()


def test_the_filter_redacts_a_traceback_that_mentions_the_token():
    try:
        raise RuntimeError(f"connect to bot{TOKEN} refused")
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.LogRecord("transport.bot", logging.ERROR, __file__, 1, "failed", (), exc_info)

    TokenRedactor().filter(record)

    assert "AAFakeToken" not in record.exc_text
    assert "<redacted>" in record.exc_text


def test_install_redaction_is_idempotent():
    install_redaction()
    install_redaction()
    for name in bot_mod._REDACTED_LOGGERS:
        count = sum(isinstance(f, TokenRedactor) for f in logging.getLogger(name).filters)
        assert count == 1, name


def test_the_token_never_reaches_the_log_through_httpx(caplog):
    caplog.set_level(logging.INFO)

    def handler(request):
        return httpx.Response(500, json={"ok": False, "error_code": 500, "description": "boom"})

    make_api(handler).send_message(OWNER_CHAT, "hi")

    assert "/sendMessage" in caplog.text  # the request was logged...
    assert TOKEN not in caplog.text       # ...but not with the token in it
    assert "AAFakeToken" not in caplog.text


def test_the_token_never_reaches_the_log_through_a_network_error(caplog):
    caplog.set_level(logging.INFO)

    def handler(request):
        raise httpx.ConnectError(f"could not reach {request.url}")

    result = make_api(handler, []).send_message(OWNER_CHAT, "hi")

    assert "AAFakeToken" not in caplog.text
    assert "AAFakeToken" not in json.dumps(result)


def test_an_empty_token_is_refused():
    with pytest.raises(ValueError):
        BotApi("")


# ------------------------------------------------------------------- outbox


def owner_store(chat: int | None = OWNER_CHAT) -> FakeStore:
    store = FakeStore()
    if chat is not None:
        store.kv[outbox_mod.OWNER_CHAT_KEY] = chat
    return store


def test_the_outbox_refuses_every_chat_but_the_owners():
    api = FakeBotApi()
    box = Outbox(api, owner_store())

    assert box.text(999, "hi") == {"ok": False, "code": "not_owner", "description": "target is not the owner's chat"}
    assert box.ask(999, "ha?", ["a"])["code"] == "not_owner"
    assert box.document(999, __file__)["code"] == "not_owner"
    assert api.calls == []


def test_the_outbox_sends_nothing_before_an_owner_is_paired():
    api = FakeBotApi()
    box = Outbox(api, owner_store(chat=None))

    assert box.text(OWNER_CHAT, "hi")["code"] == "not_configured"
    assert box.notify("hi")["code"] == "not_configured"
    assert api.calls == []


def test_notify_goes_to_the_owner_chat():
    api = FakeBotApi()
    box = Outbox(api, owner_store())

    assert box.notify("tayyor")["ok"] is True
    assert api.calls == [("send_message", {"chat_id": OWNER_CHAT, "text": "tayyor", "buttons": None, "reply_to": None})]


def test_the_chunk_limit_is_the_architecture_value():
    # Pinned as a literal: the other chunk tests read CHUNK, so they cannot catch a change to it.
    assert outbox_mod.CHUNK == 3500
    assert outbox_mod.MAX_DOCUMENT_BYTES == 45 * 1024 * 1024


def test_split_text_keeps_short_text_whole_and_fills_an_empty_text():
    assert split_text("salom") == ["salom"]
    assert split_text("") == ["·"]


def test_split_text_chunks_long_text_and_joins_back_to_the_original():
    lines = [f"qator {i} " + "a" * 90 + "\n" for i in range(200)]
    text = "".join(lines)

    parts = split_text(text)

    assert len(parts) > 1
    assert all(len(p) <= CHUNK for p in parts)
    assert "".join(parts) == text


def test_split_text_cuts_a_single_overlong_line_hard():
    line = "b" * (CHUNK * 2 + 10)

    parts = split_text(line)

    assert [len(p) for p in parts] == [CHUNK, CHUNK, 10]
    assert "".join(parts) == line


def test_text_sends_each_chunk_and_puts_the_keyboard_only_on_the_last():
    api = FakeBotApi()
    box = Outbox(api, owner_store())
    buttons = option_rows(["Ha", "Yo'q"])
    text = "x" * (CHUNK + 50)

    result = box.text(OWNER_CHAT, text, buttons=buttons)

    sent = [kwargs for name, kwargs in api.calls if name == "send_message"]
    assert len(sent) == 2
    assert sent[0]["buttons"] is None
    assert sent[1]["buttons"] == buttons
    assert "".join(s["text"] for s in sent) == text
    assert result["ok"] is True


def test_text_stops_at_the_first_failed_chunk_and_reports_it():
    api = FakeBotApi()
    api.script("send_message", {"ok": True, "result": {}}, {"ok": False, "error_code": 400, "description": "bad"})
    box = Outbox(api, owner_store())

    result = box.text(OWNER_CHAT, "y" * (CHUNK * 3))

    assert result == {"ok": False, "error_code": 400, "description": "bad"}
    assert len([m for m in api.methods() if m == "send_message"]) == 2


def test_option_rows_make_one_row_per_option_with_a_short_index_callback():
    rows = option_rows(["a", "b"])

    assert rows == [
        [{"text": "a", "callback_data": "opt:0"}],
        [{"text": "b", "callback_data": "opt:1"}],
    ]


def test_option_rows_trim_long_labels_and_cap_the_count():
    long_label = "f" * 200
    rows = option_rows([long_label] * 9)

    assert len(rows) == outbox_mod.MAX_OPTIONS
    assert rows[0][0]["text"] == "f" * 57 + "..."
    assert all(len(r[0]["callback_data"]) <= 64 for r in rows)


def test_ask_sends_the_question_with_the_option_buttons():
    api = FakeBotApi()
    box = Outbox(api, owner_store())

    box.ask(OWNER_CHAT, "Qaysi biri?", ["birinchi", "ikkinchi"])

    _, kwargs = api.calls[0]
    assert kwargs["text"] == "Qaysi biri?"
    assert kwargs["buttons"] == option_rows(["birinchi", "ikkinchi"])


def test_a_document_small_picture_goes_as_a_photo(tmp_path):
    picture = tmp_path / "shot.png"
    picture.write_bytes(b"png")
    api = FakeBotApi()

    Outbox(api, owner_store()).document(OWNER_CHAT, str(picture), "rasm")

    assert api.methods() == ["send_photo"]


def test_a_refused_photo_is_sent_again_as_a_document(tmp_path):
    picture = tmp_path / "scan.jpg"
    picture.write_bytes(b"jpg")
    api = FakeBotApi()
    api.script("send_photo", {"ok": False, "error_code": 400, "description": "PHOTO_INVALID_DIMENSIONS"})

    result = Outbox(api, owner_store()).document(OWNER_CHAT, str(picture))

    assert api.methods() == ["send_photo", "send_document"]
    assert result["ok"] is True


def test_a_pdf_goes_as_a_document_never_a_photo(tmp_path):
    report = tmp_path / "hisobot.pdf"
    report.write_bytes(b"%PDF")
    api = FakeBotApi()

    Outbox(api, owner_store()).document(OWNER_CHAT, str(report))

    assert api.methods() == ["send_document"]


def test_a_picture_over_the_photo_limit_goes_as_a_document(tmp_path, monkeypatch):
    picture = tmp_path / "big.png"
    picture.write_bytes(b"0123456789")
    monkeypatch.setattr(outbox_mod, "MAX_PHOTO_BYTES", 4)
    api = FakeBotApi()

    Outbox(api, owner_store()).document(OWNER_CHAT, str(picture))

    assert api.methods() == ["send_document"]


def test_a_file_over_the_document_limit_is_refused_before_any_send(tmp_path, monkeypatch):
    big = tmp_path / "archive.zip"
    big.write_bytes(b"0123456789")
    monkeypatch.setattr(outbox_mod, "MAX_DOCUMENT_BYTES", 4)
    api = FakeBotApi()

    result = Outbox(api, owner_store()).document(OWNER_CHAT, str(big))

    assert result["code"] == "too_large"
    assert api.calls == []


def test_a_missing_file_is_refused_before_any_send(tmp_path):
    api = FakeBotApi()

    result = Outbox(api, owner_store()).document(OWNER_CHAT, str(tmp_path / "none.pdf"))

    assert result["code"] == "file_missing"
    assert api.calls == []
