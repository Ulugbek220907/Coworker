"""OpenAI-compatible chat endpoints (dialect "openai"): DeepSeek, GLM, Groq, OpenRouter, Ollama.

Only two things are required of such an endpoint: /chat/completions and function calling.
The presets are what the settings window offers; the adapter does not care which one was
picked. The Claude presets use the Anthropic dialect and are served by llm_anthropic.py.

DeepSeek-style reasoning models may put their thinking in reasoning_content and leave the
answer empty, usually when max_tokens runs out mid-thought. The last line of that thinking
is then the best available answer, so it is salvaged rather than returning nothing. The
salvage is skipped when the reply carries tool calls, because thinking is not an answer.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from .llm_base import (
    MAX_RETRIES,
    NO_ANSWER,
    HttpChatProvider,
    Msg,
    ProviderError,
    Sleep,
    ToolCallReq,
    Turn,
    normalize_stop,
    parse_arguments,
)

# base_url is what gets "/chat/completions" appended to it. dialect selects the adapter.
PROVIDERS: dict[str, dict[str, str]] = {
    "DeepSeek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "dialect": "openai",
        "hint": "platform.deepseek.com - arzon, tez, tool-calling bor",
    },
    "GLM (BigModel)": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "dialect": "openai",
        "hint": "bigmodel.cn - glm-4-flash bepul",
    },
    "GLM (z.ai)": {
        "base_url": "https://api.z.ai/api/paas/v4",
        "model": "glm-4.5-flash",
        "dialect": "openai",
        "hint": "z.ai - xalqaro endpoint",
    },
    "Groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "dialect": "openai",
        "hint": "groq.com - bepul tier, juda tez",
    },
    "OpenRouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "deepseek/deepseek-chat-v3.1:free",
        "dialect": "openai",
        "hint": "openrouter.ai - :free modellari bor",
    },
    "Ollama (lokal)": {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
        "dialect": "openai",
        "hint": "Kompyuterning o'zida, internetsiz va butunlay bepul",
    },
    "Claude Sonnet 5.5": {
        "base_url": "https://api.anthropic.com",
        "model": "claude-sonnet-5-5",
        "dialect": "anthropic",
        "hint": "console.anthropic.com - aqlli va kuchli model",
    },
    "Claude Haiku 5.5": {
        "base_url": "https://api.anthropic.com",
        "model": "claude-haiku-5-5",
        "dialect": "anthropic",
        "hint": "console.anthropic.com - tez va arzon model",
    },
}


class OpenAICompatProvider(HttpChatProvider):
    """Chat and vision through an OpenAI-compatible /chat/completions endpoint."""

    dialect = "openai"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        name: str = "openai-compatible",
        vision_model: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        super().__init__(
            name=name,
            api_key=api_key,
            transport=transport,
            sleep=sleep,
            max_retries=max_retries,
        )
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.vision_model = vision_model or model

    async def chat(
        self,
        system: str,
        messages: list[Msg],
        tools: list[dict],
        *,
        max_tokens: int,
        temperature: float = 0.2,
    ) -> Turn:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _wire_messages(system, messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = await self.post_json(
            f"{self.base_url}/chat/completions", payload, self._headers()
        )
        choice = _first_choice(data)
        message = choice.get("message") or {}
        calls = _tool_calls(message.get("tool_calls"))
        text = str(message.get("content") or "").strip()
        if not text and not calls:
            text = _salvage(message)
        return Turn(
            text=text,
            tool_calls=calls,
            stop_reason=normalize_stop(str(choice.get("finish_reason") or "")),
            usage=data.get("usage") or {},
        )

    async def vision(self, prompt: str, image_b64: str, *, max_tokens: int = 1200) -> str:
        """One image and one prompt, sent as an image_url content part of the same endpoint.

        The budget is generous on purpose: a reasoning model that runs out of tokens
        mid-thought returns empty content, which is why the salvage applies here too.
        """
        payload = {
            "model": self.vision_model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ],
            }],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }
        data = await self.post_json(
            f"{self.base_url}/chat/completions", payload, self._headers()
        )
        message = _first_choice(data).get("message") or {}
        text = str(message.get("content") or "").strip() or _salvage(message)
        return text or NO_ANSWER

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}


def _wire_messages(system: str, messages: list[Msg]) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    if system:
        wire.append({"role": "system", "content": system})
    for msg in messages:
        if msg.role == "tool":
            item: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": msg.tool_call_id,
                "content": msg.content or "",
            }
            if msg.name:
                item["name"] = msg.name
            wire.append(item)
        elif msg.role == "assistant" and msg.tool_calls:
            wire.append({
                "role": "assistant",
                "content": msg.content or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.args, ensure_ascii=False),
                        },
                    }
                    for call in msg.tool_calls
                ],
            })
        else:
            wire.append({"role": msg.role, "content": msg.content or ""})
    return wire


def _first_choice(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise ProviderError("javob bo'sh keldi")
    return choices[0]


def _tool_calls(raw: Any) -> list[ToolCallReq]:
    calls: list[ToolCallReq] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        function = item.get("function") or {}
        calls.append(ToolCallReq(
            id=str(item.get("id") or ""),
            name=str(function.get("name") or ""),
            args=parse_arguments(function.get("arguments")),
        ))
    return calls


def _salvage(message: dict[str, Any]) -> str:
    """The last non-empty line of reasoning_content.

    Whole traces are never returned: they read to the owner as "We need to answer in Uzbek...".
    """
    lines = [
        line.strip()
        for line in str(message.get("reasoning_content") or "").splitlines()
        if line.strip()
    ]
    return lines[-1] if lines else ""


# Names the legacy modules (app.py, brain.py, ui.py) still import. They go with those modules.
LLM = OpenAICompatProvider
LLMError = ProviderError
