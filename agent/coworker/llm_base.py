"""Provider-neutral chat types and the HTTP policy both adapters share.

The orchestrator talks to a ChatProvider and never to a wire format. Each adapter
turns Msg lists into its dialect and turns the reply back into a Turn. What must be
identical for every endpoint lives here: the timeout, the retry rule, the mapping of
HTTP failures onto ProviderError, and the removal of the API key from anything that
can reach a log or an exception message.

Retry rule: 429, 5xx and transport failures are retried with backoff. Every other 4xx
is final, because the same request fails the same way again, and retrying only spends
the owner's time and quota.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol

import httpx

log = logging.getLogger("llm")

# 90 s for the whole exchange, 15 s to open the connection: a dead host should fail fast.
TIMEOUT = httpx.Timeout(90.0, connect=15.0)
MAX_RETRIES = 2
RETRY_AFTER_CAP_S = 30.0

# The stop reasons the orchestrator sees. Each adapter maps its own vocabulary onto them.
STOP_END = "end_turn"
STOP_TOOL = "tool_use"
STOP_MAX_TOKENS = "max_tokens"
_STOP_ALIASES = {"stop": STOP_END, "tool_calls": STOP_TOOL, "length": STOP_MAX_TOKENS}

# Marks arguments that were not valid JSON. The registry's validate_args rejects unexpected
# keys, so the model is told its call was malformed. The value is a flag rather than the
# raw text, so a malformed call cannot copy what the model typed into logs or the audit trail.
UNPARSED_ARGS = "_unparsed_arguments"

NO_ANSWER = "(javob olinmadi — qayta urinib ko'ring)"

Sleep = Callable[[float], Awaitable[None]]


@dataclass
class ToolCallReq:
    """One tool call the model asked for. ``args`` is always a dict."""

    id: str
    name: str
    args: dict


@dataclass
class Msg:
    """One conversation message in provider-neutral form.

    ``role`` is "user", "assistant" or "tool". An assistant message may carry
    ``tool_calls``; a tool message answers one of them through ``tool_call_id``.
    ``name`` is sent only to the OpenAI dialect, for servers that expect it on tool messages.
    """

    role: str
    content: str | None = None
    tool_calls: list[ToolCallReq] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""


@dataclass
class Turn:
    """One model reply: text, tool calls and why the model stopped.

    ``stop_reason`` is one of STOP_END, STOP_TOOL, STOP_MAX_TOKENS, or the provider's own
    value when it is outside the shared vocabulary. ``usage`` is passed through as the
    provider reported it.
    """

    text: str
    tool_calls: list[ToolCallReq]
    stop_reason: str
    usage: dict


class ProviderError(RuntimeError):
    """A failed call to a model endpoint. ``status`` is the HTTP status, or None for network failures."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ChatProvider(Protocol):
    name: str
    dialect: str  # "openai" | "anthropic"

    async def chat(
        self,
        system: str,
        messages: list[Msg],
        tools: list[dict],
        *,
        max_tokens: int,
        temperature: float = 0.2,
    ) -> Turn: ...

    async def vision(self, prompt: str, image_b64: str, *, max_tokens: int = 1200) -> str: ...

    async def close(self) -> None: ...


def normalize_stop(raw: str) -> str:
    """Map a provider's stop reason onto the shared vocabulary; unknown values pass through."""
    return _STOP_ALIASES.get(raw, raw)


def parse_arguments(raw: Any) -> dict:
    """Tool arguments as a dict, whichever form the dialect used.

    OpenAI sends a JSON string and Anthropic an object. A missing value means a call with no
    arguments. Anything that is not a JSON object is not guessed at: it becomes the
    UNPARSED_ARGS flag, so the tool never runs with input the model did not actually send.
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        if isinstance(value, dict):
            return value
    return {UNPARSED_ARGS: True}


def _retryable(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def _backoff(attempt: int) -> float:
    return 1.5 * (attempt + 1)


def _retry_after(response: httpx.Response) -> float | None:
    """A numeric Retry-After, capped so a hostile or broken server cannot park the turn."""
    try:
        value = float(response.headers.get("retry-after", ""))
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return min(max(value, 0.0), RETRY_AFTER_CAP_S)


def _decode(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise ProviderError("javob noto'g'ri formatda keldi", status=200) from exc
    if not isinstance(data, dict):
        raise ProviderError("javob noto'g'ri formatda keldi", status=200)
    return data


class HttpChatProvider:
    """HTTP client, retry loop and error mapping shared by the adapters.

    A subclass sets ``dialect``, builds the URL and headers, and translates payloads. The API
    key is sent only in a header and is removed from every message this class builds.
    """

    dialect: str = ""

    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self.name = name
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=TIMEOUT, transport=transport)
        self._sleep = sleep
        self._max_retries = max_retries

    async def close(self) -> None:
        await self._client.aclose()

    async def post_json(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        """POST a JSON payload and return the decoded 200 body.

        Every other outcome raises ProviderError. A 429, a 5xx or a transport failure is
        retried up to ``max_retries`` times; a numeric Retry-After replaces the default backoff.
        """
        last_error = ""
        status: int | None = None
        for attempt in range(self._max_retries + 1):
            retry_after: float | None = None
            try:
                response = await self._client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"tarmoq xatosi: {self._redact(str(exc))}"
                status = None
            else:
                if response.status_code == 200:
                    return _decode(response)
                last_error = self._describe(response)
                status = response.status_code
                if not _retryable(status):
                    raise ProviderError(last_error, status=status)
                retry_after = _retry_after(response)
            if attempt == self._max_retries:
                break
            delay = retry_after if retry_after is not None else _backoff(attempt)
            log.warning(
                "%s: %s, retry %d of %d in %.1f s",
                self.name, status or "network error", attempt + 1, self._max_retries, delay,
            )
            await self._sleep(delay)
        raise ProviderError(last_error, status=status)

    def _describe(self, response: httpx.Response) -> str:
        """The status and the provider's own error message, with the key removed."""
        try:
            body = response.json()
        except ValueError:
            return self._redact(f"{response.status_code}: {response.text[:200]}")
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            detail = str(error.get("message", ""))
        elif isinstance(error, str):
            detail = error
        else:
            detail = json.dumps(body, ensure_ascii=False)
        return self._redact(f"{response.status_code}: {detail[:200]}")

    def _redact(self, text: str) -> str:
        return text.replace(self._api_key, "***") if self._api_key else text


class Settings(Protocol):
    """The slice of coworker.config.Config that the model layer reads."""

    def get(self, key: str, default: Any = None) -> Any: ...


def build_provider(cfg: Settings, secrets: Callable[[str], str | None]) -> ChatProvider:
    """Choose the adapter from settings and build it.

    The dialect is ``llm_dialect`` when set. Otherwise it is the dialect of the preset whose
    base URL equals ``llm_base_url``, because the settings window writes that URL when the
    owner picks a preset. Otherwise it is "openai".

    The Anthropic adapter ignores ``llm_base_url``: it sends the key in a header, so its host
    must never come from settings. The API key is read from the secret store only; settings
    files never hold it.
    """
    # Deferred: the adapters import this module for the message types, so importing them
    # at module level would be circular.
    from .llm import PROVIDERS, OpenAICompatProvider
    from .llm_anthropic import AnthropicProvider

    base_url = str(cfg.get("llm_base_url") or "").strip().rstrip("/")
    model = str(cfg.get("llm_model") or "").strip()
    preset_name, preset = _match_preset(base_url, model, PROVIDERS)
    dialect = str(cfg.get("llm_dialect") or preset.get("dialect") or "openai").strip().lower()
    model = model or preset.get("model", "")
    api_key = secrets("llm_api_key") or ""
    name = preset_name or "custom"
    vision_model = str(cfg.get("llm_vision_model") or "").strip()

    if dialect not in ("openai", "anthropic"):
        raise ProviderError(f"LLM turi noma'lum: {dialect}")
    if dialect == "openai" and not base_url:
        raise ProviderError("LLM manzili sozlanmagan")
    if not model:
        raise ProviderError("LLM modeli sozlanmagan")
    if dialect == "anthropic":
        return AnthropicProvider(
            api_key=api_key, model=model, name=name, vision_model=vision_model
        )
    return OpenAICompatProvider(
        base_url, api_key, model, name=name, vision_model=vision_model
    )


def _match_preset(
    base_url: str, model: str, presets: Mapping[str, Mapping[str, str]]
) -> tuple[str, Mapping[str, str]]:
    """The preset for this base URL, preferring the one that also names this model.

    Several presets can share a host (the two Claude presets do), so the model breaks the tie.
    """
    if not base_url:
        return "", {}
    matches = [
        (preset_name, preset)
        for preset_name, preset in presets.items()
        if preset["base_url"].rstrip("/") == base_url
    ]
    for preset_name, preset in matches:
        if preset.get("model") == model:
            return preset_name, preset
    return matches[0] if matches else ("", {})
