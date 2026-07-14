"""Config.tool_gate: the interactive permission hook in the worker loop.

Hermetic (FakeLLMClient); proves a denial blocks the side effect, returns an
ERROR tool result to the model, and never crashes the worker (§11).
"""


from orchestrator.config import Config
from orchestrator.llm import FakeLLMClient, ToolCall, llm_response
from orchestrator.models import Subtask
from orchestrator.worker import run_worker

_WRITE = ToolCall(id="t1", name="write_file", args={"path": "x.txt", "content": "hi"})
_DONE = llm_response('{"summary": "done", "confidence": 0.9}')


def coder_llm() -> FakeLLMClient:
    return FakeLLMClient({"coder": [llm_response(tool_calls=[_WRITE]), _DONE]})


def sub() -> Subtask:
    return Subtask(id="c1", role="coder", task="write x.txt")


async def test_no_gate_preserves_headless_behavior(tmp_path):
    cfg = Config(workspace=str(tmp_path))
    out = await run_worker(coder_llm(), "coder", sub(), {}, cfg)
    assert (tmp_path / "x.txt").read_text() == "hi"
    assert out.result.summary == "done"


async def test_gate_denial_blocks_side_effect_and_reports_error(tmp_path):
    async def gate(name, args):
        return False, "user said no"

    cfg = Config(workspace=str(tmp_path), tool_gate=gate)
    out = await run_worker(coder_llm(), "coder", sub(), {}, cfg)
    assert not (tmp_path / "x.txt").exists()  # side effect blocked
    tool_msgs = [m for m in out.messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "denied by permission policy" in tool_msgs[0]["content"]
    assert "user said no" in tool_msgs[0]["content"]


async def test_gate_allow_executes_normally(tmp_path):
    seen: list[tuple[str, dict]] = []

    async def gate(name, args):
        seen.append((name, args))
        return True

    cfg = Config(workspace=str(tmp_path), tool_gate=gate)
    await run_worker(coder_llm(), "coder", sub(), {}, cfg)
    assert (tmp_path / "x.txt").read_text() == "hi"
    assert seen == [("write_file", {"path": "x.txt", "content": "hi"})]


async def test_gate_bare_bool_verdict_supported(tmp_path):
    async def gate(name, args):
        return False  # no reason tuple

    cfg = Config(workspace=str(tmp_path), tool_gate=gate)
    out = await run_worker(coder_llm(), "coder", sub(), {}, cfg)
    assert not (tmp_path / "x.txt").exists()
    tool_msgs = [m for m in out.messages if m.get("role") == "tool"]
    assert tool_msgs[0]["content"] == "ERROR: denied by permission policy"


async def test_gate_exception_degrades_to_denial_not_crash(tmp_path):
    async def gate(name, args):
        raise RuntimeError("UI fell over")

    cfg = Config(workspace=str(tmp_path), tool_gate=gate)
    out = await run_worker(coder_llm(), "coder", sub(), {}, cfg)
    assert not (tmp_path / "x.txt").exists()
    tool_msgs = [m for m in out.messages if m.get("role") == "tool"]
    assert "permission gate failed" in tool_msgs[0]["content"]
    assert out.result.summary == "done"  # worker completed anyway


async def test_denial_emits_tool_call_event_with_error(tmp_path):
    events: list[tuple[str, dict]] = []

    class Spy:
        def __getattr__(self, name):
            def note(**f):
                events.append((name, f))
            return note

    async def gate(name, args):
        return False, "nope"

    cfg = Config(workspace=str(tmp_path), tool_gate=gate)
    await run_worker(coder_llm(), "coder", sub(), {}, cfg, observer=Spy())
    tool_events = [f for ev, f in events if ev == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0]["error"] and "nope" in tool_events[0]["error"]
