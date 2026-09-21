"""Minimal async client for any OpenAI-compatible chat endpoint.

Kept provider-agnostic on purpose: DeepSeek today, a local Ollama model or a
free GLM tier tomorrow, without touching the rest of the codebase. Only two
things are actually required of the endpoint - ``/chat/completions`` and
function calling.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

log = logging.getLogger("llm")

# Presets the settings window offers. base_url is what gets "/chat/completions"
# appended to it.
PROVIDERS: dict[str, dict[str, str]] = {
    "DeepSeek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "hint": "platform.deepseek.com - arzon, tez, tool-calling bor",
    },
    "GLM (BigModel)": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "hint": "bigmodel.cn - glm-4-flash bepul",
    },
    "GLM (z.ai)": {
        "base_url": "https://api.z.ai/api/paas/v4",
        "model": "glm-4.5-flash",
        "hint": "z.ai - xalqaro endpoint",
    },
    "Groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "hint": "groq.com - bepul tier, juda tez",
    },
    "OpenRouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "deepseek/deepseek-chat-v3.1:free",
        "hint": "openrouter.ai - :free modellari bor",
    },
    "Ollama (lokal)": {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
        "hint": "Kompyuterning o'zida, internetsiz va butunlay bepul",
    },
}


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0))

    async def close(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 700,
        retries: int = 2,
    ) -> dict:
        """One completion. Returns the assistant message dict."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        last_error = ""
        for attempt in range(retries + 1):
            try:
                r = await self._client.post(
                    f"{self.base_url}/chat/completions", json=payload, headers=headers
                )
                if r.status_code == 200:
                    data = r.json()
                    choices = data.get("choices") or []
                    if not choices:
                        raise LLMError("javob bo'sh keldi")
                    return choices[0].get("message", {})

                last_error = _describe(r)
                # 4xx other than rate limiting will not fix themselves.
                if r.status_code < 500 and r.status_code != 429:
                    raise LLMError(last_error)
            except httpx.HTTPError as exc:
                last_error = f"tarmoq xatosi: {exc}"

            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))

        raise LLMError(last_error or "noma'lum xato")

    async def vision(
        self,
        prompt: str,
        image_b64: str,
        *,
        model: str,
        max_tokens: int = 500,
    ) -> str:
        """One image + prompt call, against a vision-capable model.

        A reasoning model (deepseek-flash) sometimes leaves `content` empty and
        puts its answer in `reasoning_content`, so fall back to that rather than
        returning nothing.
        """
        payload = {
            "model": model,
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
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        r = await self._client.post(
            f"{self.base_url}/chat/completions", json=payload, headers=headers
        )
        if r.status_code != 200:
            raise LLMError(_describe(r))
        choices = r.json().get("choices") or []
        if not choices:
            raise LLMError("vision javobi bo'sh")
        msg = choices[0].get("message", {})
        text = (msg.get("content") or "").strip()
        if not text:
            text = (msg.get("reasoning_content") or "").strip()
        return text or "(bo'sh javob)"

    async def ping(self) -> tuple[bool, str]:
        """Used by the settings window's Test button."""
        try:
            msg = await self.chat(
                [{"role": "user", "content": "ping"}], max_tokens=5, retries=0
            )
            return True, (msg.get("content") or "ok")[:60]
        except Exception as exc:
            return False, str(exc)[:200]


def _describe(r: httpx.Response) -> str:
    try:
        body = r.json()
        err = body.get("error")
        if isinstance(err, dict):
            return f"{r.status_code}: {err.get('message', '')}"
        if isinstance(err, str):
            return f"{r.status_code}: {err}"
        return f"{r.status_code}: {json.dumps(body, ensure_ascii=False)[:200]}"
    except Exception:
        return f"{r.status_code}: {r.text[:200]}"
