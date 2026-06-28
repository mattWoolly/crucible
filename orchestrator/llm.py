"""LLM client abstraction (SPEC §4, §10, §11).

The orchestrator depends only on the ``LLMClient`` protocol. The real client
(``OpenAIClient``) targets any OpenAI-compatible endpoint — in particular
MiniMax's — via the ``openai`` SDK. Tests use ``FakeLLMClient`` so no unit
test ever hits the network (§12: no live LLM tests).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, Sequence, runtime_checkable

from .constants import DEFAULT_LLM_RETRIES
from .errors import LLMError
from .json_extract import extract_json


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    latency_ms: float = 0.0
    model: str = "default"


@runtime_checkable
class LLMClient(Protocol):
    async def complete(
        self,
        messages: Sequence[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse: ...


# --------------------------------------------------------------------------
# Real client (OpenAI-compatible: MiniMax)
# --------------------------------------------------------------------------
_RETRYABLE_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "RateLimitError",
    "InternalServerError",
    "APIError",
}


def _is_retryable(exc: BaseException) -> bool:
    # Match by class name so we don't hard-depend on openai's exception tree
    # shape, and also retry on 5xx/429 carried via a status_code attribute.
    if type(exc).__name__ in _RETRYABLE_NAMES:
        status = getattr(exc, "status_code", None)
        if status is None:
            return True
        return status == 429 or 500 <= int(status) < 600
    return False


class OpenAIClient:
    """Thin wrapper over ``openai.AsyncOpenAI`` with retry/backoff (§11)."""

    def __init__(
        self,
        api_key: str | None,
        base_url: str | None,
        model: str,
        *,
        retries: int = DEFAULT_LLM_RETRIES,
        backoff_base: float = 0.5,
        client: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.model = model
        self.retries = retries
        self.backoff_base = backoff_base
        self._sleep = sleep
        if client is not None:
            self._client = client
        else:  # pragma: no cover - exercised only with a real endpoint
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def complete(
        self,
        messages: Sequence[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools

        attempt = 0
        start = time.monotonic()
        while True:
            try:
                resp = await self._client.chat.completions.create(**kwargs)
                return self._parse(resp, (time.monotonic() - start) * 1000)
            except Exception as exc:  # noqa: BLE001 - classified below
                attempt += 1
                if attempt > self.retries or not _is_retryable(exc):
                    raise LLMError(f"LLM call failed after {attempt} attempt(s): {exc}") from exc
                await self._sleep(self.backoff_base * (2 ** (attempt - 1)))

    def _parse(self, resp: Any, latency_ms: float) -> LLMResponse:
        choice = resp.choices[0]
        msg = choice.message
        tool_calls: list[ToolCall] = []
        for tc in getattr(msg, "tool_calls", None) or []:
            raw_args = tc.function.arguments or "{}"
            parsed = extract_json(raw_args) or {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, args=parsed))
        usage_obj = getattr(resp, "usage", None)
        usage = Usage(
            prompt_tokens=getattr(usage_obj, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage_obj, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage_obj, "total_tokens", 0) or 0,
        )
        return LLMResponse(
            content=msg.content or "",
            tool_calls=tool_calls,
            usage=usage,
            latency_ms=latency_ms,
            model=getattr(resp, "model", self.model),
        )


# --------------------------------------------------------------------------
# Fake client (tests)
# --------------------------------------------------------------------------
ScriptFn = Callable[[str, Sequence[dict]], LLMResponse]


class FakeLLMClient:
    """Deterministic, network-free LLM for tests.

    ``scripts`` maps a role name to a list of ``LLMResponse`` returned in order
    on successive calls for that role; alternatively pass a callable
    ``(role, messages) -> LLMResponse``. The active role is taken from the
    system message's ``_role`` marker the orchestrator injects, falling back to
    ``"default"``.
    """

    def __init__(
        self,
        scripts: dict[str, list[LLMResponse]] | ScriptFn | None = None,
        *,
        default: LLMResponse | None = None,
    ) -> None:
        self._scripts = scripts
        self._default = default or LLMResponse(content='{"summary": "ok", "confidence": 0.9}')
        self._counters: dict[str, int] = {}
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def role_of(messages: Sequence[dict]) -> str:
        for m in messages:
            if m.get("role") == "system" and "_role" in m:
                return m["_role"]
        return "default"

    async def complete(
        self,
        messages: Sequence[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        role = self.role_of(messages)
        self.calls.append({"role": role, "messages": list(messages), "temperature": temperature})

        if callable(self._scripts):
            return self._scripts(role, messages)
        if isinstance(self._scripts, dict) and role in self._scripts:
            seq = self._scripts[role]
            i = self._counters.get(role, 0)
            self._counters[role] = i + 1
            if i < len(seq):
                return seq[i]
            return seq[-1] if seq else self._default
        return self._default


def llm_response(content: str = "", *, tool_calls: list[ToolCall] | None = None,
                 total_tokens: int = 10) -> LLMResponse:
    """Convenience builder for tests."""
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        usage=Usage(total_tokens=total_tokens),
    )
