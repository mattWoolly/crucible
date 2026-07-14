"""AnthropicClient: OpenAI-shape <-> Messages API translation (hermetic —
the SDK client is injected, never constructed)."""

from types import SimpleNamespace

import pytest

from orchestrator.anthropic_client import (
    AnthropicClient,
    to_anthropic_messages,
    to_anthropic_tools,
)
from orchestrator.errors import LLMError

# -- fakes -------------------------------------------------------------------


def _block(**kw):
    return SimpleNamespace(**kw)


def fake_response(blocks=None, input_tokens=100, output_tokens=20, model="claude-opus-4-8"):
    return SimpleNamespace(
        content=blocks if blocks is not None else [_block(type="text", text="hello")],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason="end_turn",
        model=model,
    )


class FakeSDK:
    """Mimics anthropic.AsyncAnthropic just enough: .messages.create(**kwargs)."""

    def __init__(self, responses=None, error=None):
        self.calls: list[dict] = []
        self._responses = list(responses or [fake_response()])
        self._error = error
        outer = self

        async def create(**kwargs):
            outer.calls.append(kwargs)
            if outer._error is not None:
                raise outer._error
            return outer._responses.pop(0) if len(outer._responses) > 1 else outer._responses[0]

        self.messages = SimpleNamespace(create=create)


# -- message translation -------------------------------------------------------


def test_system_message_becomes_system_param_and_role_marker_dropped():
    system, msgs = to_anthropic_messages([
        {"role": "system", "content": "you are a coder", "_role": "coder"},
        {"role": "user", "content": "do the thing"},
    ])
    assert system == "you are a coder"
    assert msgs == [{"role": "user", "content": "do the thing"}]


def test_assistant_tool_calls_become_tool_use_blocks():
    _, msgs = to_anthropic_messages([
        {"role": "user", "content": "t"},
        {
            "role": "assistant",
            "content": "on it",
            "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
            ],
        },
    ])
    blocks = msgs[1]["content"]
    assert blocks[0] == {"type": "text", "text": "on it"}
    assert blocks[1] == {"type": "tool_use", "id": "t1", "name": "read_file",
                         "input": {"path": "a.py"}}


def test_consecutive_tool_results_merge_into_one_user_turn():
    _, msgs = to_anthropic_messages([
        {"role": "user", "content": "t"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
            {"id": "t2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
        {"role": "tool", "tool_call_id": "t2", "content": "r2"},
    ])
    # exactly one user turn carrying BOTH tool_result blocks (Anthropic requires this)
    assert msgs[-1]["role"] == "user"
    results = msgs[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert all(r["type"] == "tool_result" for r in results)
    assert len(msgs) == 3


def test_malformed_tool_arguments_become_empty_input():
    _, msgs = to_anthropic_messages([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "a", "arguments": "{broken"}},
        ]},
    ])
    assert msgs[0]["content"][0]["input"] == {}


def test_tool_schema_conversion():
    tools = to_anthropic_tools([{
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "run a command",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                           "required": ["command"]},
        },
    }])
    assert tools == [{
        "name": "run_shell",
        "description": "run a command",
        "input_schema": {"type": "object", "properties": {"command": {"type": "string"}},
                         "required": ["command"]},
    }]


# -- the client ---------------------------------------------------------------


async def test_complete_sends_system_tools_and_no_temperature():
    sdk = FakeSDK()
    client = AnthropicClient(model="claude-opus-4-8", client=sdk)
    await client.complete(
        [{"role": "system", "content": "S", "_role": "coder"},
         {"role": "user", "content": "U"}],
        tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
        temperature=0.7,  # engine passes it; must NOT be forwarded (400 on Opus 4.8)
    )
    call = sdk.calls[0]
    assert "temperature" not in call and "top_p" not in call
    assert call["system"] == "S"
    assert call["model"] == "claude-opus-4-8"
    assert call["max_tokens"] > 0
    assert call["tools"][0]["name"] == "f"


async def test_complete_parses_text_tools_and_usage():
    sdk = FakeSDK(responses=[fake_response(
        blocks=[_block(type="text", text="working"),
                _block(type="tool_use", id="t9", name="write_file",
                       input={"path": "x", "content": "y"})],
        input_tokens=1000, output_tokens=200,
    )])
    client = AnthropicClient(client=sdk)
    resp = await client.complete([{"role": "user", "content": "u"}])
    assert resp.content == "working"
    assert resp.tool_calls[0].id == "t9"
    assert resp.tool_calls[0].args == {"path": "x", "content": "y"}
    assert resp.usage.total_tokens == 1200
    assert resp.model == "claude-opus-4-8"


async def test_refusal_returns_empty_content_not_crash():
    refusal = fake_response(blocks=[])
    refusal.stop_reason = "refusal"
    client = AnthropicClient(client=FakeSDK(responses=[refusal]))
    resp = await client.complete([{"role": "user", "content": "u"}])
    assert resp.content == "" and resp.tool_calls == []


async def test_non_retryable_error_wraps_in_llmerror():
    class BadRequestError(Exception):
        status_code = 400

    client = AnthropicClient(client=FakeSDK(error=BadRequestError("no")), retries=3)
    with pytest.raises(LLMError, match="after 1 attempt"):
        await client.complete([{"role": "user", "content": "u"}])


async def test_retryable_error_retries_then_raises():
    class APITimeoutError(Exception):
        pass

    sleeps: list[float] = []

    async def note_sleep(s):
        sleeps.append(s)

    client = AnthropicClient(client=FakeSDK(error=APITimeoutError("hang")),
                             retries=2, sleep=note_sleep)
    with pytest.raises(LLMError, match="after 3 attempt"):
        await client.complete([{"role": "user", "content": "u"}])
    assert len(sleeps) == 2  # backed off between attempts


async def test_rate_limit_uses_dedicated_budget():
    class RateLimitError(Exception):
        status_code = 429

    sleeps: list[float] = []

    async def note_sleep(s):
        sleeps.append(s)

    client = AnthropicClient(client=FakeSDK(error=RateLimitError("429")),
                             retries=1, rate_limit_retries=3,
                             rate_limit_backoff_base=1.0, sleep=note_sleep)
    with pytest.raises(LLMError, match="rate-limited"):
        await client.complete([{"role": "user", "content": "u"}])
    assert len(sleeps) == 3  # rate-limit budget, not the generic retries=1
