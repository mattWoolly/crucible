"""Anthropic backend for the ``LLMClient`` protocol (official SDK, native
Messages API — deliberately NOT the OpenAI-compat path).

The engine speaks OpenAI Chat Completions shapes internally (messages with
``tool_calls``, ``role: "tool"`` results, function-tool schemas). This module
translates that to/from Anthropic's Messages API:

  system message            -> the ``system`` request param (``_role`` marker dropped)
  assistant ``tool_calls``  -> ``tool_use`` content blocks
  ``role: "tool"`` results  -> ``tool_result`` blocks, consecutive ones merged
                               into ONE user turn (parallel results must land
                               in a single message per Anthropic docs)
  function-tool schema      -> ``{name, description, input_schema}``

Model-behavior notes (current Claude models — Opus 4.7+, Sonnet 5, Fable 5):
  * ``temperature``/``top_p``/``top_k`` are REJECTED with a 400, so the
    engine's per-role temperature is intentionally not forwarded.
  * The ``thinking`` param is omitted → the model runs without extended
    thinking. Adaptive thinking would require replaying thinking blocks
    verbatim across turns, which the engine's OpenAI-shaped message store
    can't carry yet (recorded as an open thread in EVOLUTION.md).
  * A safety decline returns HTTP 200 with ``stop_reason: "refusal"`` — the
    worker sees empty content and degrades per §11 rather than crashing.

Retry policy: the SDK's own retries are disabled (``max_retries=0``); the
engine's backoff loop (same shape as ``OpenAIClient``) is the single retry
authority, with the dedicated long rate-limit budget.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, Awaitable, Callable, Sequence

from .constants import (
    DEFAULT_LLM_RETRIES,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_RATE_LIMIT_BACKOFF_S,
    DEFAULT_RATE_LIMIT_RETRIES,
    DEFAULT_REQUEST_TIMEOUT_S,
    RATE_LIMIT_BACKOFF_CAP_S,
)
from .errors import LLMError
from .llm import LLMResponse, ToolCall, Usage

DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"
DEFAULT_MAX_OUTPUT_TOKENS = 16_000  # non-streaming sweet spot per Anthropic docs

_RETRYABLE_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "RateLimitError",
    "InternalServerError",
    "OverloadedError",
    "APIStatusError",
}


def _is_retryable(exc: BaseException) -> bool:
    if type(exc).__name__ in _RETRYABLE_NAMES:
        status = getattr(exc, "status_code", None)
        if status is None:
            return True
        return status == 429 or int(status) >= 500
    return False


def _is_rate_limit(exc: BaseException) -> bool:
    return type(exc).__name__ == "RateLimitError" or getattr(exc, "status_code", None) == 429


# -- request translation -----------------------------------------------------
def to_anthropic_tools(tools: Sequence[dict]) -> list[dict]:
    """OpenAI function-tool schemas -> Anthropic tool definitions."""
    out = []
    for t in tools:
        fn = t.get("function", {})
        out.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


def to_anthropic_messages(messages: Sequence[dict]) -> tuple[str | None, list[dict]]:
    """OpenAI-shaped history -> (system, anthropic messages)."""
    system_parts: list[str] = []
    out: list[dict] = []

    def _append_tool_result(tool_msg: dict) -> None:
        block = {
            "type": "tool_result",
            "tool_use_id": tool_msg.get("tool_call_id", ""),
            "content": str(tool_msg.get("content", "")),
        }
        # Merge consecutive tool results into one user turn (parallel calls).
        if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
            out[-1]["content"].append(block)
        else:
            out.append({"role": "user", "content": [block]})

    for m in messages:
        role = m.get("role")
        if role == "system":
            system_parts.append(str(m.get("content", "")))
        elif role == "tool":
            _append_tool_result(m)
        elif role == "assistant" and m.get("tool_calls"):
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": str(m["content"])})
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    }
                )
            out.append({"role": "assistant", "content": blocks})
        elif role in ("user", "assistant"):
            out.append({"role": role, "content": str(m.get("content", ""))})
        # unknown roles are dropped silently — nothing in the engine emits them
    system = "\n\n".join(p for p in system_parts if p) or None
    return system, out


class AnthropicClient:
    """``LLMClient`` implementation over ``anthropic.AsyncAnthropic``."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        *,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        retries: int = DEFAULT_LLM_RETRIES,
        backoff_base: float = 0.5,
        rate_limit_retries: int = DEFAULT_RATE_LIMIT_RETRIES,
        rate_limit_backoff_base: float = DEFAULT_RATE_LIMIT_BACKOFF_S,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        client: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.retries = retries
        self.backoff_base = backoff_base
        self.rate_limit_retries = rate_limit_retries
        self.rate_limit_backoff_base = rate_limit_backoff_base
        self._sem = asyncio.Semaphore(max_concurrency)
        self._sleep = sleep
        if client is not None:
            self._client = client
        else:  # pragma: no cover - exercised only with a real endpoint
            from anthropic import AsyncAnthropic

            # api_key=None lets the SDK resolve ANTHROPIC_API_KEY /
            # ANTHROPIC_AUTH_TOKEN / an `ant auth login` profile itself.
            self._client = AsyncAnthropic(
                api_key=api_key, timeout=request_timeout_s, max_retries=0
            )

    async def complete(
        self,
        messages: Sequence[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.0,  # accepted for protocol parity; NOT forwarded
    ) -> LLMResponse:
        system, amessages = to_anthropic_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "messages": amessages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = to_anthropic_tools(tools)

        attempt = 0
        rl_attempt = 0
        start = time.monotonic()
        while True:
            try:
                async with self._sem:
                    resp = await self._client.messages.create(**kwargs)
                return self._parse(resp, (time.monotonic() - start) * 1000)
            except Exception as exc:  # noqa: BLE001 - classified below
                if _is_rate_limit(exc):
                    rl_attempt += 1
                    if rl_attempt > self.rate_limit_retries:
                        raise LLMError(
                            f"Anthropic call failed after {rl_attempt} rate-limited attempt(s): {exc}"
                        ) from exc
                    delay = min(
                        self.rate_limit_backoff_base * (2 ** (rl_attempt - 1)),
                        RATE_LIMIT_BACKOFF_CAP_S,
                    )
                    await self._sleep(delay + random.uniform(0, delay / 4))  # noqa: S311
                    continue
                attempt += 1
                if attempt > self.retries or not _is_retryable(exc):
                    raise LLMError(f"Anthropic call failed after {attempt} attempt(s): {exc}") from exc
                await self._sleep(self.backoff_base * (2 ** (attempt - 1)))

    def _parse(self, resp: Any, latency_ms: float) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in getattr(resp, "content", None) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, args=dict(block.input or {}))
                )
        usage_obj = getattr(resp, "usage", None)
        prompt = getattr(usage_obj, "input_tokens", 0) or 0
        completion = getattr(usage_obj, "output_tokens", 0) or 0
        # A refusal is a valid HTTP 200: empty/partial content, no tool calls.
        # Returned as-is — extract_worker_result degrades it (§11).
        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            usage=Usage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
            ),
            latency_ms=latency_ms,
            model=getattr(resp, "model", self.model),
        )
