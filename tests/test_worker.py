import pytest

from orchestrator.config import Config
from orchestrator.constants import DEGRADED_CONFIDENCE
from orchestrator.llm import FakeLLMClient, ToolCall, llm_response
from orchestrator.models import Subtask, WorkerResult
from orchestrator.worker import (
    enforce_context_budget,
    render_user_task,
    revise_worker,
    run_worker,
)


def test_render_includes_upstream_summary_and_artifacts():
    sub = Subtask(id="c1", role="coder", task="apply migration", depends_on=["r1"])
    deps = {"r1": WorkerResult(summary="found token v1 in auth.py", artifacts={"path": "auth.py"}, confidence=0.9)}
    text = render_user_task(sub, deps)
    assert "apply migration" in text
    assert "found token v1" in text
    assert "auth.py" in text
    assert "Context from prior steps" in text


def test_render_flags_low_confidence_dependency():
    sub = Subtask(id="c1", role="coder", task="t", depends_on=["r1"])
    deps = {"r1": WorkerResult(summary="weak", confidence=DEGRADED_CONFIDENCE)}
    text = render_user_task(sub, deps)
    assert "LOW CONFIDENCE" in text


def test_render_caps_and_drops_lowest_confidence_first():
    sub = Subtask(id="c", role="coder", task="t", depends_on=["a", "b"])
    deps = {
        "a": WorkerResult(summary="A" * 100, confidence=0.9),
        "b": WorkerResult(summary="B" * 100, confidence=0.2),
    }
    text = render_user_task(sub, deps, cap=180)
    # The low-confidence 'b' block should be dropped first.
    assert "AAAA" in text
    assert "BBBB" not in text


def test_enforce_context_budget_trims_tool_not_system_or_task():
    messages = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "the original task"},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "tool", "tool_call_id": "1", "content": "X" * 5000},
    ]
    out = enforce_context_budget(messages, budget_chars=200)
    assert out[0]["content"] == "S"  # system preserved
    assert out[1]["content"] == "the original task"  # task preserved
    assert "trimmed" in out[3]["content"]  # tool result trimmed


async def test_worker_emits_final_json_immediately():
    fake = FakeLLMClient({"researcher": [llm_response('{"summary": "done reading", "confidence": 0.8}')]})
    config = Config(workspace="/tmp")
    sub = Subtask(id="r1", role="researcher", task="read stuff")
    out = await run_worker(fake, "researcher", sub, {}, config)
    assert out.result.summary == "done reading"
    assert out.result.confidence == 0.8


async def test_worker_calls_tool_then_answers(tmp_path):
    (tmp_path / "auth.py").write_text("token_v1 = 1")
    fake = FakeLLMClient({
        "researcher": [
            llm_response(tool_calls=[ToolCall(id="t1", name="read_file", args={"path": "auth.py"})]),
            llm_response('{"summary": "read auth.py", "artifacts": {"path": "auth.py"}, "confidence": 0.9}'),
        ]
    })
    config = Config(workspace=str(tmp_path))
    sub = Subtask(id="r1", role="researcher", task="inspect auth.py")
    out = await run_worker(fake, "researcher", sub, {}, config)
    assert out.result.summary == "read auth.py"
    # The tool result must have entered the message history.
    assert any(m.get("role") == "tool" for m in out.messages)


async def test_worker_max_steps_fallback_parses_last_content():
    # Always returns a tool call -> never terminates by content; max_steps hit.
    fake = FakeLLMClient({
        "researcher": [llm_response(
            '{"summary": "last words", "confidence": 0.5}',
            tool_calls=[ToolCall(id="t", name="list_files", args={"path": "."})],
        )]
    })
    config = Config(workspace="/tmp", max_steps=2)
    sub = Subtask(id="r1", role="researcher", task="loop forever")
    out = await run_worker(fake, "researcher", sub, {}, config)
    assert out.result.summary == "last words"


async def test_revise_keeps_prior_context():
    fake = FakeLLMClient({"coder": [
        llm_response('{"summary": "v1", "confidence": 0.5}'),
        llm_response('{"summary": "v2 improved", "confidence": 0.9}'),
    ]})
    config = Config(workspace="/tmp")
    sub = Subtask(id="c1", role="coder", task="do")
    first = await run_worker(fake, "coder", sub, {}, config)
    revised = await revise_worker(
        fake, "coder", sub, first.messages, ["bad"], ["fix it"], config
    )
    assert revised.result.summary == "v2 improved"
    # Feedback message appended to the prior context.
    assert any("critic" in m["content"].lower() for m in revised.messages if m["role"] == "user")
