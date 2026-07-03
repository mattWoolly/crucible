import json

import pytest

from orchestrator import Config, FinalReport, NoopObserver, Orchestrator
from orchestrator.errors import PlanValidationError
from orchestrator.llm import FakeLLMClient, llm_response
from orchestrator.observers import Observer


class RecordingObserver(Observer):
    def __init__(self):
        self.events = []

    def __getattribute__(self, name):
        # Record every known event call.
        if name in (
            "run_started", "plan_ready", "subtask_started", "llm_call",
            "tool_call", "critic_score", "subtask_finished", "run_finished", "flush",
        ):
            def rec(**f):
                self.events.append(name)
            return rec
        return object.__getattribute__(self, name)


def _plan_json(subtasks):
    return json.dumps({"reasoning": "decompose", "subtasks": subtasks})


def _wr(summary, conf=0.9):
    return llm_response(f'{{"summary": "{summary}", "confidence": {conf}}}')


def _approve():
    return llm_response('{"score": 9, "approved": true, "issues": [], "suggestions": []}')


def _config(tmp_path, **kw):
    kw.setdefault("observer", NoopObserver())
    return Config(workspace=str(tmp_path), **kw)


async def test_happy_path_end_to_end(tmp_path):
    plan = _plan_json([
        {"id": "r1", "role": "researcher", "task": "research tokens", "depends_on": [], "inputs": ""},
        {"id": "c1", "role": "coder", "task": "apply migration", "depends_on": ["r1"], "inputs": ""},
    ])
    fake = FakeLLMClient({
        "planner": [llm_response(plan)],
        "researcher": [_wr("found token v1")],
        "coder": [_wr("migrated and tested")],
        "synthesizer": [_wr("final merged answer")],
        "critic": [_approve()],
    })
    orch = Orchestrator(fake, _config(tmp_path))
    report = await orch.run("migrate tokens v1->v2")
    assert isinstance(report, FinalReport)
    assert report.summary.startswith("final merged answer")
    assert report.confidence == 0.9
    # researcher, coder, and synthesizer results are present.
    assert "r1" in report.subtask_results and "c1" in report.subtask_results
    # The augmented synthesizer result is also recorded (auto_synth).
    assert any(k.startswith("auto_synth") for k in report.subtask_results)
    assert report.tokens_total > 0


async def test_planner_cycle_triggers_corrective_retry(tmp_path):
    bad = _plan_json([
        {"id": "a", "role": "coder", "task": "t", "depends_on": ["b"], "inputs": ""},
        {"id": "b", "role": "coder", "task": "t", "depends_on": ["a"], "inputs": ""},
    ])
    good = _plan_json([
        {"id": "a", "role": "coder", "task": "t", "depends_on": [], "inputs": ""},
    ])
    fake = FakeLLMClient({
        "planner": [llm_response(bad), llm_response(good)],
        "coder": [_wr("done")],
        "synthesizer": [_wr("merged")],
        "critic": [_approve()],
    })
    orch = Orchestrator(fake, _config(tmp_path))
    report = await orch.run("task")
    assert report.summary.startswith("merged")
    # The planner was called twice (corrective retry after the cycle).
    planner_calls = [c for c in fake.calls if c["role"] == "planner"]
    assert len(planner_calls) == 2
    # The 2nd planner call carried the corrective defect message.
    sys2 = next(m for m in planner_calls[1]["messages"] if m["role"] == "system")
    assert "invalid" in sys2["content"]


async def test_planner_gets_workspace_orientation(tmp_path):
    (tmp_path / "PROJECT.md").write_text("spec")
    plan = _plan_json([
        {"id": "a", "role": "coder", "task": "t", "depends_on": [], "inputs": ""},
    ])
    fake = FakeLLMClient({
        "planner": [llm_response(plan)],
        "coder": [_wr("done")],
        "synthesizer": [_wr("merged")],
        "critic": [_approve()],
    })
    orch = Orchestrator(fake, _config(tmp_path))
    await orch.run("the task")
    planner_user = next(
        m for c in fake.calls if c["role"] == "planner"
        for m in c["messages"] if m["role"] == "user"
    )
    assert "Workspace orientation" in planner_user["content"]
    assert "PROJECT.md" in planner_user["content"]
    assert "the task" in planner_user["content"]


async def test_planner_no_json_retry_says_no_tools(tmp_path):
    # First response mimics an agentic model emitting pseudo tool calls (no
    # JSON at all); the corrective retry must tell it it has no tools.
    pseudo_tools = llm_response("<think>let me explore</think>\n<tool_call>ls -la</tool_call>")
    good = _plan_json([
        {"id": "a", "role": "coder", "task": "t", "depends_on": [], "inputs": ""},
    ])
    fake = FakeLLMClient({
        "planner": [pseudo_tools, llm_response(good)],
        "coder": [_wr("done")],
        "synthesizer": [_wr("merged")],
        "critic": [_approve()],
    })
    orch = Orchestrator(fake, _config(tmp_path))
    report = await orch.run("task")
    assert report.summary.startswith("merged")
    planner_calls = [c for c in fake.calls if c["role"] == "planner"]
    assert len(planner_calls) == 2
    sys2 = next(m for m in planner_calls[1]["messages"] if m["role"] == "system")
    assert "no JSON object" in sys2["content"]
    assert "NO tools" in sys2["content"]


class _SynthCrashLLM:
    """Delegates to a FakeLLMClient but raises LLMError for the synthesizer —
    models a provider quota dying mid-run (e.g. 429 rate limit)."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = inner.calls

    async def complete(self, messages, tools=None, temperature=0.2):
        from orchestrator.errors import LLMError
        role = next((m.get("_role") for m in messages if m.get("_role")), None)
        if role == "synthesizer":
            raise LLMError("LLM call failed after 4 attempt(s): 429 rate limit")
        return await self.inner.complete(messages, tools=tools, temperature=temperature)


async def test_synthesizer_llm_error_degrades_instead_of_crashing(tmp_path):
    plan = _plan_json([
        {"id": "c1", "role": "coder", "task": "write code", "depends_on": [], "inputs": ""},
    ])
    fake = FakeLLMClient({
        "planner": [llm_response(plan)],
        "coder": [_wr("wrote the code")],
        "critic": [_approve()],
    })
    rec = RecordingObserver()
    orch = Orchestrator(_SynthCrashLLM(fake), _config(tmp_path, observer=rec))
    report = await orch.run("task")  # must NOT raise
    # Worker results survive; the report is degraded, not lost.
    assert report.subtask_results["c1"].summary == "wrote the code"
    assert report.confidence <= 0.1
    assert "synthesis failed" in report.summary
    assert "run_finished" in rec.events


async def test_planner_unrepairable_raises_but_emits_run_finished(tmp_path):
    bad = _plan_json([
        {"id": "a", "role": "coder", "task": "t", "depends_on": ["a"], "inputs": ""},
    ])
    fake = FakeLLMClient({"planner": [llm_response(bad)]})
    rec = RecordingObserver()
    orch = Orchestrator(fake, _config(tmp_path, observer=rec, max_plan_retries=1))
    with pytest.raises(PlanValidationError):
        await orch.run("task")
    # run_finished + flush still happen via finally (§9, §11).
    assert "run_finished" in rec.events
    assert "flush" in rec.events


async def test_one_worker_raises_degrades_and_continues(tmp_path):
    plan = _plan_json([
        {"id": "r1", "role": "researcher", "task": "good one", "depends_on": [], "inputs": ""},
        {"id": "r2", "role": "researcher", "task": "boom one", "depends_on": [], "inputs": ""},
        {"id": "c1", "role": "coder", "task": "apply", "depends_on": ["r1", "r2"], "inputs": ""},
    ])

    def script(role, messages):
        user = next((m for m in messages if m["role"] == "user"), {"content": ""})
        if role == "planner":
            return llm_response(plan)
        if role == "researcher" and "boom" in user["content"]:
            raise RuntimeError("simulated worker crash")
        if role == "researcher":
            return _wr("good research")
        if role == "coder":
            return _wr("coded")
        if role == "synthesizer":
            return _wr("merged with one degraded input")
        return _approve()

    fake = FakeLLMClient(script)
    orch = Orchestrator(fake, _config(tmp_path))
    report = await orch.run("task")
    # The crashed sibling became a degraded result; the run still produced an answer.
    assert report.subtask_results["r2"].is_degraded
    assert not report.subtask_results["r1"].is_degraded
    assert report.summary.startswith("merged with one degraded input")


async def test_budget_exhausted_degrades_gracefully(tmp_path):
    plan = _plan_json([
        {"id": "r1", "role": "researcher", "task": "research", "depends_on": [], "inputs": ""},
    ])
    fake = FakeLLMClient({
        "planner": [llm_response(plan)],
        "researcher": [_wr("research")],
        "synthesizer": [_wr("merged from partial")],
        "critic": [_approve()],
    })
    rec = RecordingObserver()
    # Planner consumes the only allowed call; worker layer is skipped.
    orch = Orchestrator(fake, _config(tmp_path, observer=rec, max_llm_calls=1))
    report = await orch.run("task")
    assert report.confidence <= 0.1  # capped to degraded
    assert "budget exhausted" in report.summary
    assert "run_finished" in rec.events
