import asyncio
import types

import pytest

from orchestrator.errors import LLMError
from orchestrator.llm import (
    FakeLLMClient,
    LLMResponse,
    OpenAIClient,
    ToolCall,
    Usage,
    llm_response,
)


async def test_fake_scripts_per_role_in_order():
    fake = FakeLLMClient(
        {
            "researcher": [llm_response('{"summary": "first"}'), llm_response('{"summary": "second"}')],
        }
    )
    msgs = [{"role": "system", "_role": "researcher", "content": "x"}]
    r1 = await fake.complete(msgs)
    r2 = await fake.complete(msgs)
    assert r1.content == '{"summary": "first"}'
    assert r2.content == '{"summary": "second"}'
    assert len(fake.calls) == 2
    assert fake.calls[0]["role"] == "researcher"


async def test_fake_default_when_unscripted():
    fake = FakeLLMClient({})
    r = await fake.complete([{"role": "system", "_role": "coder", "content": "x"}])
    assert "summary" in r.content


async def test_fake_callable_script():
    def script(role, messages):
        return llm_response(f'{{"summary": "{role}"}}')

    fake = FakeLLMClient(script)
    r = await fake.complete([{"role": "system", "_role": "synthesizer", "content": "x"}])
    assert r.content == '{"summary": "synthesizer"}'


# --- OpenAIClient retry/backoff via injected fake transport ---------------
class _Boom(Exception):
    def __init__(self, status_code=503):
        super().__init__("boom")
        self.status_code = status_code


_Boom.__name__ = "InternalServerError"  # match retryable classification


class _FakeChat:
    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0
        self.completions = types.SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise _Boom()
        msg = types.SimpleNamespace(content='{"summary": "ok"}', tool_calls=None)
        choice = types.SimpleNamespace(message=msg)
        return types.SimpleNamespace(
            choices=[choice],
            usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3),
            model="MiniMax-Text-01",
        )


class _FakeOpenAI:
    def __init__(self, fail_times):
        self.chat = _FakeChat(fail_times)


async def test_openai_client_retries_then_succeeds():
    fake = _FakeOpenAI(fail_times=2)
    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)

    client = OpenAIClient(None, None, "MiniMax-Text-01", client=fake, sleep=fake_sleep)
    resp = await client.complete([{"role": "user", "content": "hi"}])
    assert resp.content == '{"summary": "ok"}'
    assert resp.usage.total_tokens == 3
    assert fake.chat.calls == 3  # 2 failures + 1 success
    assert sleeps == [0.5, 1.0]  # exponential backoff


async def test_openai_client_raises_after_exhausting_retries():
    fake = _FakeOpenAI(fail_times=99)

    async def fake_sleep(d):
        return None

    client = OpenAIClient(None, None, "m", retries=2, client=fake, sleep=fake_sleep)
    with pytest.raises(LLMError):
        await client.complete([{"role": "user", "content": "hi"}])


async def test_openai_client_parses_tool_calls():
    def make_resp():
        fn = types.SimpleNamespace(name="read_file", arguments='{"path": "a.py"}')
        tc = types.SimpleNamespace(id="call_1", function=fn)
        msg = types.SimpleNamespace(content="", tool_calls=[tc])
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=msg)],
            usage=types.SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            model="m",
        )

    class C:
        def __init__(self):
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=self._create)
            )

        async def _create(self, **kwargs):
            return make_resp()

    client = OpenAIClient(None, None, "m", client=C())
    resp = await client.complete([{"role": "user", "content": "hi"}])
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "read_file"
    assert resp.tool_calls[0].args == {"path": "a.py"}


# --- rate-limit-aware retry + concurrency cap ------------------------------
class _RateLimited(Exception):
    def __init__(self):
        super().__init__("429 Token Plan rate limit reached")
        self.status_code = 429


_RateLimited.__name__ = "RateLimitError"  # match retryable classification


class _FlakyRateLimitedOpenAI:
    """429s a fixed number of times, then succeeds."""

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )

    async def _create(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise _RateLimited()
        msg = types.SimpleNamespace(content='{"summary": "ok"}', tool_calls=None)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=msg)],
            usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model="m",
        )


async def test_rate_limit_gets_longer_backoff_and_own_retry_budget():
    # 5 consecutive 429s would exhaust the generic budget (retries=3); the
    # dedicated rate-limit budget must survive them, with much longer waits.
    fake = _FlakyRateLimitedOpenAI(fail_times=5)
    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)

    client = OpenAIClient(None, None, "m", client=fake, sleep=fake_sleep)
    resp = await client.complete([{"role": "user", "content": "hi"}])
    assert resp.content == '{"summary": "ok"}'
    assert fake.calls == 6
    assert len(sleeps) == 5
    # Exponential from 10s, capped at 60s, plus up to 25% jitter.
    for got, base in zip(sleeps, [10.0, 20.0, 40.0, 60.0, 60.0]):
        assert base <= got <= base * 1.25, f"backoff {got} outside [{base}, {base*1.25}]"


async def test_rate_limit_exhaustion_still_raises():
    fake = _FlakyRateLimitedOpenAI(fail_times=99)

    async def fake_sleep(d):
        return None

    client = OpenAIClient(None, None, "m", rate_limit_retries=2, client=fake, sleep=fake_sleep)
    with pytest.raises(LLMError):
        await client.complete([{"role": "user", "content": "hi"}])
    assert fake.calls == 3  # initial + 2 rate-limit retries


async def test_max_concurrency_caps_parallel_requests():
    state = {"active": 0, "peak": 0}

    class _SlowOpenAI:
        def __init__(self):
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=self._create)
            )

        async def _create(self, **kwargs):
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            await asyncio.sleep(0.005)
            state["active"] -= 1
            msg = types.SimpleNamespace(content="ok", tool_calls=None)
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=msg)],
                usage=types.SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                model="m",
            )

    client = OpenAIClient(None, None, "m", client=_SlowOpenAI(), max_concurrency=3)
    await asyncio.gather(*[
        client.complete([{"role": "user", "content": "hi"}]) for _ in range(10)
    ])
    assert state["peak"] <= 3
