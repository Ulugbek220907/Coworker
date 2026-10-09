"""Tests for the LLM adapters: wire shapes, parsing, retries, error mapping and selection.

Every request goes through httpx.MockTransport, so nothing reaches the network. The retry
sleep is replaced by a recorder, so retry tests run instantly and can assert the delays.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest

from coworker import llm_base
from coworker.llm import LLM, LLMError, PROVIDERS, OpenAICompatProvider
from coworker.llm_anthropic import AnthropicProvider
from coworker.llm_base import (
    NO_ANSWER,
    UNPARSED_ARGS,
    Msg,
    ProviderError,
    ToolCallReq,
    Turn,
    build_provider,
    parse_arguments,
)
from coworker.tools.registry import validate_args

API_KEY = "sk-test-0123456789-SECRET"
RESULT = '{"ok": true, "files": ["a.txt"]}'

TOOLS_OPENAI = [{"type": "function", "function": {
    "name": "list_dir",
    "description": "List a folder.",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"]},
}}]
TOOLS_ANTHROPIC = [{
    "name": "list_dir",
    "description": "List a folder.",
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                     "required": ["path"]},
}]


@pytest.fixture(autouse=True)
def coworker_home(tmp_path, monkeypatch):
    """Settings and the store live under COWORKER_HOME; keep any accidental use in tmp_path."""
    monkeypatch.setenv("COWORKER_HOME", str(tmp_path / "coworker"))


class Script:
    """Replays scripted responses in order and records every request it receives."""

    def __init__(self, *items: httpx.Response | Exception) -> None:
        self._items = list(items)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._items:
            raise AssertionError(f"unexpected request to {request.url}")
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def body(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


class Sleeps:
    """Records the delays the adapters asked for instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def reply(status: int, body: Any = None, *, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def ok_reply(dialect: str, text: str) -> httpx.Response:
    """A well-formed 200 reply in the given dialect."""
    if dialect == "openai":
        return reply(200, openai_body(text))
    return reply(200, anthropic_body([{"type": "text", "text": text}]))


def openai_provider(script: Script, sleeps: Sleeps | None = None, **kwargs: Any) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        "https://llm.example.test/v1/", API_KEY, "chat-model",
        transport=httpx.MockTransport(script), sleep=sleeps or Sleeps(), **kwargs,
    )


def anthropic_provider(script: Script, sleeps: Sleeps | None = None, **kwargs: Any) -> AnthropicProvider:
    return AnthropicProvider(
        api_key=API_KEY, model="claude-sonnet-5-5",
        transport=httpx.MockTransport(script), sleep=sleeps or Sleeps(), **kwargs,
    )


FACTORIES = {"openai": openai_provider, "anthropic": anthropic_provider}


def openai_body(content: str | None = None, *, tool_calls: list | None = None,
                finish: str = "stop", reasoning: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 3},
    }


def openai_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def anthropic_body(blocks: list[dict[str, Any]], *, stop: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "content": blocks,
        "stop_reason": stop, "usage": {"input_tokens": 12, "output_tokens": 5},
    }


def chat(provider: Any, messages: list[Msg], *, system: str = "", tools: list[dict] | None = None,
         max_tokens: int = 300) -> Turn:
    return asyncio.run(provider.chat(system, messages, tools or [], max_tokens=max_tokens))


def vision(provider: Any, prompt: str, image_b64: str, **kwargs: Any) -> str:
    return asyncio.run(provider.vision(prompt, image_b64, **kwargs))


def close(provider: Any) -> None:
    asyncio.run(provider.close())


# ----------------------------------------------------------------- request shapes

def test_openai_request_shape_carries_tools_choice_max_tokens_and_system_message():
    script = Script(reply(200, openai_body("ok")))
    turn = chat(openai_provider(script), [Msg("user", "salom")], system="Sen yordamchisan.",
                tools=TOOLS_OPENAI, max_tokens=300)

    request = script.requests[0]
    body = script.body()
    assert str(request.url) == "https://llm.example.test/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert body["model"] == "chat-model"
    assert body["max_tokens"] == 300
    assert body["temperature"] == 0.2
    assert body["tools"] == TOOLS_OPENAI
    assert body["tool_choice"] == "auto"
    assert body["messages"] == [
        {"role": "system", "content": "Sen yordamchisan."},
        {"role": "user", "content": "salom"},
    ]
    assert turn.text == "ok"


def test_openai_without_tools_or_system_sends_neither():
    script = Script(reply(200, openai_body("ok")))
    chat(openai_provider(script), [Msg("user", "hi")])

    body = script.body()
    assert "tools" not in body
    assert "tool_choice" not in body
    assert body["messages"] == [{"role": "user", "content": "hi"}]


def test_anthropic_request_shape_puts_system_at_top_level_and_always_sends_max_tokens():
    script = Script(reply(200, anthropic_body([{"type": "text", "text": "Salom"}])))
    turn = chat(anthropic_provider(script), [Msg("user", "salom")], system="Sen yordamchisan.",
                tools=TOOLS_ANTHROPIC, max_tokens=400)

    request = script.requests[0]
    body = script.body()
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == API_KEY
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in request.headers
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] == 400
    assert body["system"] == "Sen yordamchisan."
    assert body["messages"] == [{"role": "user", "content": "salom"}]
    assert body["tools"] == TOOLS_ANTHROPIC
    assert body["tool_choice"] == {"type": "auto"}
    assert turn.text == "Salom"


def test_anthropic_without_tools_or_system_omits_both_and_keeps_max_tokens():
    script = Script(reply(200, anthropic_body([{"type": "text", "text": "ok"}])))
    chat(anthropic_provider(script), [Msg("user", "hi")], max_tokens=50)

    body = script.body()
    assert body["max_tokens"] == 50
    assert "system" not in body
    assert "tools" not in body
    assert "tool_choice" not in body


def test_timeout_is_ninety_seconds_for_the_request():
    provider = openai_provider(Script())
    assert provider._client.timeout.read == 90.0
    close(provider)


# ------------------------------------------------------------------- parsing

def test_openai_parses_tool_calls_usage_and_normalises_stop_reason():
    script = Script(reply(200, openai_body(
        None, tool_calls=[openai_call("call_1", "list_dir", '{"path": "C:/Docs"}')],
        finish="tool_calls")))
    turn = chat(openai_provider(script), [Msg("user", "papka")], tools=TOOLS_OPENAI)

    assert turn.text == ""
    assert turn.tool_calls == [ToolCallReq("call_1", "list_dir", {"path": "C:/Docs"})]
    assert turn.stop_reason == llm_base.STOP_TOOL
    assert turn.usage == {"prompt_tokens": 9, "completion_tokens": 3}


@pytest.mark.parametrize(("finish", "expected"), [
    ("stop", llm_base.STOP_END),
    ("length", llm_base.STOP_MAX_TOKENS),
    ("content_filter", "content_filter"),
])
def test_openai_stop_reasons_map_onto_the_shared_vocabulary(finish, expected):
    script = Script(reply(200, openai_body("x", finish=finish)))
    assert chat(openai_provider(script), [Msg("user", "hi")]).stop_reason == expected


def test_anthropic_parses_text_and_tool_use_blocks():
    script = Script(reply(200, anthropic_body([
        {"type": "text", "text": "Ochaman."},
        {"type": "tool_use", "id": "toolu_9", "name": "list_dir", "input": {"path": "C:/x"}},
    ], stop="tool_use")))
    turn = chat(anthropic_provider(script), [Msg("user", "papka")], tools=TOOLS_ANTHROPIC)

    assert turn.text == "Ochaman."
    assert turn.tool_calls == [ToolCallReq("toolu_9", "list_dir", {"path": "C:/x"})]
    assert turn.stop_reason == llm_base.STOP_TOOL
    assert turn.usage == {"input_tokens": 12, "output_tokens": 5}


@pytest.mark.parametrize(("stop", "expected"), [
    ("end_turn", llm_base.STOP_END),
    ("max_tokens", llm_base.STOP_MAX_TOKENS),
])
def test_anthropic_stop_reasons_pass_through_the_shared_vocabulary(stop, expected):
    script = Script(reply(200, anthropic_body([{"type": "text", "text": "x"}], stop=stop)))
    assert chat(anthropic_provider(script), [Msg("user", "hi")]).stop_reason == expected


def test_malformed_tool_arguments_are_flagged_and_fail_registry_validation():
    script = Script(reply(200, openai_body(
        None, tool_calls=[openai_call("call_1", "list_dir", "{path: C:/")], finish="tool_calls")))
    turn = chat(openai_provider(script), [Msg("user", "x")], tools=TOOLS_OPENAI)

    call = turn.tool_calls[0]
    assert call.args == {UNPARSED_ARGS: True}
    assert validate_args(TOOLS_OPENAI[0]["function"]["parameters"], call.args) is not None


@pytest.mark.parametrize(("raw", "expected"), [
    ("", {}),
    (None, {}),
    ({"a": 1}, {"a": 1}),
    ('{"a": 1}', {"a": 1}),
    ("[1, 2]", {UNPARSED_ARGS: True}),
    ("{bad", {UNPARSED_ARGS: True}),
])
def test_parse_arguments_accepts_both_dialect_forms_and_flags_the_rest(raw, expected):
    assert parse_arguments(raw) == expected


def test_openai_reasoning_is_salvaged_only_without_an_answer_or_a_tool_call():
    script = Script(
        reply(200, openai_body("", reasoning="Thinking...\nWe answer in Uzbek.\nFayllar topildi")),
        reply(200, openai_body("", reasoning="Plan", finish="tool_calls",
                               tool_calls=[openai_call("c1", "list_dir", '{"path": "C:/"}')])),
    )
    provider = openai_provider(script)
    salvaged = chat(provider, [Msg("user", "a")])
    with_tool = chat(provider, [Msg("user", "b")])

    assert salvaged.text == "Fayllar topildi"
    assert with_tool.text == ""
    assert len(with_tool.tool_calls) == 1


# ------------------------------------------------------------ tool round trips

def test_openai_round_trip_wire_format_sends_tool_calls_and_results_back():
    script = Script(reply(200, openai_body("Hujjatlar: a.txt")))
    history = [
        Msg("user", "papkani ko'rsat"),
        Msg("assistant", None, tool_calls=[ToolCallReq("call_1", "list_dir", {"path": "C:/Docs"})]),
        Msg("tool", RESULT, tool_call_id="call_1", name="list_dir"),
    ]
    chat(openai_provider(script), history, system="sys", tools=TOOLS_OPENAI)

    sent = script.body()["messages"]
    assert sent[0] == {"role": "system", "content": "sys"}
    assert sent[2] == {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "list_dir", "arguments": '{"path": "C:/Docs"}'}},
    ]}
    assert sent[3] == {"role": "tool", "tool_call_id": "call_1", "content": RESULT,
                       "name": "list_dir"}


def test_anthropic_groups_results_and_drops_empty_text_blocks():
    script = Script(reply(200, anthropic_body([{"type": "text", "text": "done"}])))
    history = [
        Msg("user", "a"),
        Msg("assistant", "", tool_calls=[
            ToolCallReq("t1", "list_dir", {"path": "C:/a"}),
            ToolCallReq("t2", "list_dir", {"path": "C:/b"}),
        ]),
        Msg("tool", "r1", tool_call_id="t1"),
        Msg("tool", "r2", tool_call_id="t2"),
        Msg("assistant", "done"),
        Msg("user", ""),
    ]
    chat(anthropic_provider(script), history, tools=TOOLS_ANTHROPIC)

    assert script.body()["messages"] == [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "list_dir", "input": {"path": "C:/a"}},
            {"type": "tool_use", "id": "t2", "name": "list_dir", "input": {"path": "C:/b"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "r1"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "r2"},
        ]},
        {"role": "assistant", "content": "done"},
    ]


@pytest.mark.parametrize("dialect", ["openai", "anthropic"])
def test_multi_turn_tool_round_trip_completes_in_both_dialects(dialect):
    call_id = {"openai": "call_1", "anthropic": "toolu_1"}[dialect]
    if dialect == "openai":
        script = Script(
            reply(200, openai_body(None, finish="tool_calls", tool_calls=[
                openai_call(call_id, "list_dir", '{"path": "C:/Docs"}')])),
            reply(200, openai_body("Hujjatlar: a.txt")),
        )
    else:
        script = Script(
            reply(200, anthropic_body([
                {"type": "text", "text": "Ko'raman."},
                {"type": "tool_use", "id": call_id, "name": "list_dir",
                 "input": {"path": "C:/Docs"}},
            ], stop="tool_use")),
            reply(200, anthropic_body([{"type": "text", "text": "Hujjatlar: a.txt"}])),
        )
    provider = FACTORIES[dialect](script)

    async def go() -> tuple[Turn, Turn]:
        try:
            first = await provider.chat("sys", [Msg("user", "papkani ko'rsat")], TOOLS_OPENAI,
                                        max_tokens=200)
            history = [
                Msg("user", "papkani ko'rsat"),
                Msg("assistant", first.text, tool_calls=first.tool_calls),
                Msg("tool", RESULT, tool_call_id=call_id, name="list_dir"),
            ]
            second = await provider.chat("sys", history, TOOLS_OPENAI, max_tokens=200)
            return first, second
        finally:
            await provider.close()

    first, second = asyncio.run(go())
    assert first.tool_calls == [ToolCallReq(call_id, "list_dir", {"path": "C:/Docs"})]
    assert second.text == "Hujjatlar: a.txt"
    assert second.tool_calls == []
    assert second.stop_reason == llm_base.STOP_END

    sent = script.body(1)["messages"]
    if dialect == "openai":
        # The system prompt is the first wire message, so the history starts at index 1.
        assert sent[2]["tool_calls"][0]["id"] == call_id
        assert sent[3] == {"role": "tool", "tool_call_id": call_id, "content": RESULT,
                           "name": "list_dir"}
    else:
        assert sent[1] == {"role": "assistant", "content": [
            {"type": "text", "text": "Ko'raman."},
            {"type": "tool_use", "id": call_id, "name": "list_dir", "input": {"path": "C:/Docs"}},
        ]}
        assert sent[2] == {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": RESULT},
        ]}


# -------------------------------------------------------- retries and errors

@pytest.mark.parametrize("dialect", ["openai", "anthropic"])
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_other_client_errors_are_final_and_never_retried(dialect, status):
    sleeps = Sleeps()
    # The second item would succeed if the adapter retried, so a retry would hide the error.
    script = Script(reply(status, {"error": {"message": "nope"}}), ok_reply(dialect, "ok"))
    provider = FACTORIES[dialect](script, sleeps)

    with pytest.raises(ProviderError) as info:
        chat(provider, [Msg("user", "hi")])
    assert info.value.status == status
    assert str(info.value) == f"{status}: nope"
    assert len(script.requests) == 1
    assert sleeps.delays == []


@pytest.mark.parametrize("dialect", ["openai", "anthropic"])
def test_429_is_retried_once_then_succeeds(dialect):
    sleeps = Sleeps()
    script = Script(reply(429, {"error": {"message": "slow down"}}), ok_reply(dialect, "ok"))
    turn = chat(FACTORIES[dialect](script, sleeps), [Msg("user", "hi")])

    assert turn.text == "ok"
    assert len(script.requests) == 2
    assert sleeps.delays == [1.5]


def test_5xx_is_retried_with_growing_backoff_then_mapped():
    sleeps = Sleeps()
    script = Script(reply(500, {"error": {"message": "boom"}}),
                    reply(503, {"error": {"message": "busy"}}),
                    reply(502, {"error": {"message": "gateway"}}))
    with pytest.raises(ProviderError) as info:
        chat(openai_provider(script, sleeps), [Msg("user", "hi")])

    assert info.value.status == 502
    assert len(script.requests) == 3
    assert sleeps.delays == [1.5, 3.0]


@pytest.mark.parametrize(("header", "expected"), [
    ("7", 7.0),
    ("9999", 30.0),
    ("soon", 1.5),
])
def test_retry_after_sets_the_delay_capped_and_falls_back_when_unreadable(header, expected):
    sleeps = Sleeps()
    script = Script(reply(429, {"error": {"message": "slow"}}, headers={"retry-after": header}),
                    reply(200, openai_body("ok")))
    chat(openai_provider(script, sleeps), [Msg("user", "hi")])

    assert sleeps.delays == [expected]


def test_transport_failures_are_retried_then_mapped_without_a_status():
    sleeps = Sleeps()
    script = Script(*(httpx.ConnectError("connection refused") for _ in range(3)))
    with pytest.raises(ProviderError) as info:
        chat(openai_provider(script, sleeps), [Msg("user", "hi")])

    assert info.value.status is None
    assert str(info.value).startswith("tarmoq xatosi")
    assert len(script.requests) == 3
    assert sleeps.delays == [1.5, 3.0]


@pytest.mark.parametrize(("dialect", "body"), [
    ("openai", {"choices": []}),
    ("anthropic", {"type": "error"}),
])
def test_empty_or_unexpected_200_bodies_raise_provider_error(dialect, body):
    script = Script(reply(200, body))
    with pytest.raises(ProviderError, match="javob bo'sh keldi"):
        chat(FACTORIES[dialect](script), [Msg("user", "hi")])


def test_non_json_200_body_raises_provider_error_with_status_200():
    script = Script(httpx.Response(200, content=b"<html>maintenance</html>"))
    with pytest.raises(ProviderError) as info:
        chat(openai_provider(script), [Msg("user", "hi")])
    assert info.value.status == 200


# ------------------------------------------------------------------- vision

def test_openai_vision_sends_one_image_part_to_the_vision_model():
    script = Script(reply(200, openai_body("Ekranda Word oynasi.")))
    provider = openai_provider(script, vision_model="vision-model")
    answer = vision(provider, "Nima ko'rinyapti?", "QUJD", max_tokens=900)

    body = script.body()
    assert str(script.requests[0].url) == "https://llm.example.test/v1/chat/completions"
    assert body["model"] == "vision-model"
    assert body["max_tokens"] == 900
    assert body["temperature"] == 0.1
    assert "tools" not in body
    assert body["messages"][0]["content"] == [
        {"type": "text", "text": "Nima ko'rinyapti?"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
    ]
    assert answer == "Ekranda Word oynasi."


def test_openai_vision_falls_back_to_the_chat_model_and_salvages_reasoning():
    script = Script(reply(200, openai_body("", reasoning="Hmm.\nEkranda faktura ko'rinadi")),
                    reply(200, openai_body("")))
    provider = openai_provider(script)

    assert vision(provider, "q", "QUJD") == "Ekranda faktura ko'rinadi"
    assert script.body(0)["model"] == "chat-model"
    assert vision(provider, "q", "QUJD") == NO_ANSWER


def test_anthropic_vision_sends_a_base64_image_block_and_default_budget():
    script = Script(reply(200, anthropic_body([{"type": "text", "text": " Ekranda jadval. "}])))
    answer = vision(anthropic_provider(script), "Nima?", "QUJD")

    body = script.body()
    assert str(script.requests[0].url) == "https://api.anthropic.com/v1/messages"
    assert script.requests[0].headers["x-api-key"] == API_KEY
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] == 1200
    assert body["messages"] == [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": "QUJD"}},
        {"type": "text", "text": "Nima?"},
    ]}]
    assert answer == "Ekranda jadval."


# ------------------------------------------------------------ build_provider

def secrets_from(values: dict[str, str]):
    return lambda name: values.get(name)


def test_deepseek_settings_select_the_openai_adapter_and_key_comes_from_secrets():
    cfg = {"llm_base_url": "https://api.deepseek.com", "llm_model": "deepseek-chat"}
    provider = build_provider(cfg, secrets_from({"llm_api_key": API_KEY}))

    assert isinstance(provider, OpenAICompatProvider)
    assert provider.dialect == "openai"
    assert provider.name == "DeepSeek"
    assert provider.base_url == "https://api.deepseek.com"
    assert provider.model == "deepseek-chat"
    assert provider._headers() == {"Authorization": f"Bearer {API_KEY}"}
    close(provider)


def test_claude_preset_selects_the_anthropic_adapter_by_matching_model():
    cfg = {"llm_base_url": "https://api.anthropic.com", "llm_model": "claude-haiku-5-5"}
    provider = build_provider(cfg, secrets_from({"llm_api_key": API_KEY}))

    assert isinstance(provider, AnthropicProvider)
    assert provider.dialect == "anthropic"
    assert provider.name == "Claude Haiku 5.5"
    assert provider.model == "claude-haiku-5-5"
    close(provider)


def test_explicit_dialect_setting_wins_over_the_preset():
    cfg = {"llm_dialect": "openai", "llm_base_url": "https://api.anthropic.com",
           "llm_model": "some-model"}
    provider = build_provider(cfg, secrets_from({}))

    assert isinstance(provider, OpenAICompatProvider)
    close(provider)


def test_anthropic_dialect_ignores_a_configured_host_so_the_key_never_leaves_anthropic():
    cfg = {"llm_dialect": "anthropic", "llm_base_url": "https://collector.example",
           "llm_model": "claude-sonnet-5-5"}
    provider = build_provider(cfg, secrets_from({"llm_api_key": API_KEY}))

    assert isinstance(provider, AnthropicProvider)
    assert provider._headers()["x-api-key"] == API_KEY
    close(provider)


def test_api_key_is_read_from_secrets_only_never_from_settings():
    cfg = {"llm_base_url": "https://api.deepseek.com", "llm_api_key": "sk-from-settings-file"}
    provider = build_provider(cfg, secrets_from({}))

    assert provider._headers() == {}
    close(provider)


def test_unknown_dialect_and_missing_host_are_refused():
    with pytest.raises(ProviderError, match="LLM turi noma'lum"):
        build_provider({"llm_dialect": "gemini", "llm_base_url": "https://x.example"},
                       secrets_from({}))
    with pytest.raises(ProviderError, match="LLM manzili sozlanmagan"):
        build_provider({}, secrets_from({}))
    with pytest.raises(ProviderError, match="LLM modeli sozlanmagan"):
        build_provider({"llm_base_url": "http://localhost:9999/v1"}, secrets_from({}))


def test_unknown_host_without_a_preset_is_named_custom():
    provider = build_provider({"llm_base_url": "http://localhost:9999/v1", "llm_model": "m"},
                              secrets_from({}))
    assert provider.name == "custom"
    close(provider)


def test_claude_presets_use_the_anthropic_dialect_with_the_expected_model_ids():
    assert PROVIDERS["Claude Sonnet 5.5"]["dialect"] == "anthropic"
    assert PROVIDERS["Claude Sonnet 5.5"]["model"] == "claude-sonnet-5-5"
    assert PROVIDERS["Claude Haiku 5.5"]["dialect"] == "anthropic"
    assert PROVIDERS["Claude Haiku 5.5"]["model"] == "claude-haiku-5-5"


def test_legacy_names_still_import_and_build_with_the_old_signature():
    client = LLM("https://api.deepseek.com", API_KEY, "deepseek-chat")

    assert isinstance(client, OpenAICompatProvider)
    assert LLMError is ProviderError
    close(client)


# ------------------------------------------------------------------ secrecy

@pytest.mark.parametrize("dialect", ["openai", "anthropic"])
def test_api_key_never_reaches_logs_or_error_messages(dialect, caplog):
    caplog.set_level(logging.DEBUG)
    echoed = {"error": {"type": "error", "message": f"rejected key {API_KEY}"}}
    script = Script(reply(429, echoed), reply(400, echoed))
    provider = FACTORIES[dialect](script)

    with pytest.raises(ProviderError) as info:
        chat(provider, [Msg("user", "hi")])

    assert "retry 1 of 2" in caplog.text
    assert "400: rejected key ***" == str(info.value)
    assert API_KEY not in str(info.value)
    assert API_KEY not in caplog.text


def test_api_key_is_removed_from_transport_error_text_and_logs(caplog):
    caplog.set_level(logging.DEBUG)
    script = Script(*(httpx.ConnectError(f"refused while sending {API_KEY}") for _ in range(3)))
    provider = openai_provider(script)

    with pytest.raises(ProviderError) as info:
        chat(provider, [Msg("user", "hi")])

    assert "***" in str(info.value)
    assert API_KEY not in str(info.value)
    assert API_KEY not in caplog.text
