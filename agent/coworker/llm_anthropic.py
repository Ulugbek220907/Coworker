"""Anthropic Messages API adapter (dialect "anthropic").

What this module absorbs, so the orchestrator never sees it:
- the system prompt is a top-level field, not a message;
- max_tokens is required on every request;
- a tool call is a tool_use block, and its result goes back as a tool_result block inside
  a user message; consecutive results share one user message;
- an image is a base64 block, not a data URL;
- the key travels in x-api-key, so the host is a constant here and is never read from
  settings. A configured host would let the key be sent anywhere.
"""
from __future__ import annotations

import asyncio
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

ANTHROPIC_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(HttpChatProvider):
    """Chat and vision through the Anthropic Messages API."""

    dialect = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        name: str = "anthropic",
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
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": _wire_messages(messages),
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = {"type": "auto"}
        data = await self.post_json(f"{ANTHROPIC_URL}/v1/messages", payload, self._headers())
        return _turn_from(data)

    async def vision(self, prompt: str, image_b64: str, *, max_tokens: int = 1200) -> str:
        payload: dict[str, Any] = {
            "model": self.vision_model,
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg", "data": image_b64,
                    }},
                    {"type": "text", "text": prompt},
                ],
            }],
        }
        data = await self.post_json(f"{ANTHROPIC_URL}/v1/messages", payload, self._headers())
        text = "".join(
            str(block.get("text") or "")
            for block in _blocks(data)
            if block.get("type") == "text"
        ).strip()
        return text or NO_ANSWER

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key, "anthropic-version": ANTHROPIC_VERSION}


def _wire_messages(messages: list[Msg]) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    # The user message currently collecting tool_result blocks, if any.
    results: list[dict[str, Any]] | None = None
    for msg in messages:
        if msg.role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": msg.tool_call_id,
                "content": msg.content or "",
            }
            if results is None:
                results = []
                wire.append({"role": "user", "content": results})
            results.append(block)
            continue
        results = None
        if msg.role == "assistant" and msg.tool_calls:
            blocks: list[dict[str, Any]] = []
            if msg.content:
                blocks.append({"type": "text", "text": msg.content})
            blocks.extend(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.args}
                for call in msg.tool_calls
            )
            wire.append({"role": "assistant", "content": blocks})
        elif msg.content:
            # The API rejects empty text blocks, so an empty turn is dropped rather than sent.
            wire.append({"role": msg.role, "content": msg.content})
    return wire


def _blocks(data: dict[str, Any]) -> list[dict[str, Any]]:
    content = data.get("content")
    if not isinstance(content, list):
        raise ProviderError("javob bo'sh keldi")
    return [block for block in content if isinstance(block, dict)]


def _turn_from(data: dict[str, Any]) -> Turn:
    blocks = _blocks(data)
    text = "".join(
        str(block.get("text") or "") for block in blocks if block.get("type") == "text"
    ).strip()
    calls = [
        ToolCallReq(
            id=str(block.get("id") or ""),
            name=str(block.get("name") or ""),
            args=parse_arguments(block.get("input")),
        )
        for block in blocks
        if block.get("type") == "tool_use"
    ]
    return Turn(
        text=text,
        tool_calls=calls,
        stop_reason=normalize_stop(str(data.get("stop_reason") or "")),
        usage=data.get("usage") or {},
    )
