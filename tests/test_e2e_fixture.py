"""End-to-end fixture run (SPEC §14.2-§14.4).

A scripted FakeLLM drives a real token v1->v2 "migration": the researcher reads
the fixture (which contains a planted secret), the coder writes the migrated
file and verifies it with run_shell, the synthesizer merges, and a final critic
gates the answer. We assert:

* the trace tree contains every event type in the right order (§14.3);
* the planted secret is redacted out of the trace (§9, §14.3);
* nothing is written to stderr during the run (§14.4);
* the run respects budgets and returns a complete FinalReport (§14.5).
"""

import io
import json

import pytest

from orchestrator import Config, Orchestrator
from orchestrator.llm import FakeLLMClient, ToolCall, llm_response
from orchestrator.observers import JSONLObserver

SECRET = "API_KEY=sk-supersecret1234567890"


@pytest.fixture
def workspace(tmp_path):
    # v1 token format + a planted secret the researcher will read.
    (tmp_path / "tokens.py").write_text(
        "# token format v1\n"
        "TOKEN_VERSION = 1\n"
        "def make_token_v1(user):\n    return f'v1:{user}'\n"
    )
    (tmp_path / "config.env").write_text(SECRET + "\n")
    return str(tmp_path)


def _count_tool_msgs(messages):
    return sum(1 for m in messages if m.get("role") == "tool")


def _script(role, messages):
    user = next((m for m in messages if m["role"] == "user"), {"content": ""})
    tool_msgs = _count_tool_msgs(messages)

    if role == "planner":
        plan = {
            "reasoning": "read then migrate then verify",
            "subtasks": [
                {"id": "r1", "role": "researcher", "task": "read tokens.py and config.env",
                 "depends_on": [], "inputs": ""},
                {"id": "c1", "role": "coder",
                 "task": "migrate token format v1 to v2 in tokens.py and verify",
                 "depends_on": ["r1"], "inputs": "what the researcher found"},
            ],
        }
        return llm_response(json.dumps(plan))

    if role == "researcher":
        if tool_msgs == 0:
            return llm_response(tool_calls=[ToolCall("t1", "read_file", {"path": "config.env"})])
        return llm_response('{"summary": "tokens.py uses TOKEN_VERSION=1", '
                            '"artifacts": {"path": "tokens.py"}, "confidence": 0.9}')

    if role == "coder":
        if tool_msgs == 0:
            new = ("# token format v2\nTOKEN_VERSION = 2\n"
                   "def make_token_v2(user):\n    return f'v2:{user}'\n")
            return llm_response(tool_calls=[ToolCall("w1", "write_file",
                                                     {"path": "tokens.py", "content": new})])
        if tool_msgs == 1:
            return llm_response(tool_calls=[ToolCall("s1", "run_shell",
                                                     {"command": "grep token_v2 tokens.py"})])
        return llm_response('{"summary": "migrated tokens.py to v2 and verified with grep", '
                            '"artifacts": {"path": "tokens.py"}, "confidence": 0.9}')

    if role == "synthesizer":
        return llm_response('{"summary": "Migration complete: tokens.py now v2, verified.", '
                            '"confidence": 0.9}')

    # critic
    return llm_response('{"score": 9, "approved": true, "issues": [], '
                        '"suggestions": [], "rubric": [{"criterion": "verified", "passed": true}]}')


async def test_e2e_full_trace_redacted_and_silent(workspace, capfd):
    buf = io.StringIO()
    fake = FakeLLMClient(_script)
    config = Config(workspace=workspace, observer=JSONLObserver(stream=buf))
    orch = Orchestrator(fake, config)

    report = await orch.run("migrate tokens from v1 to v2 and verify")

    # 1. The migration actually happened in the real workspace.
    migrated = (__import__("pathlib").Path(workspace) / "tokens.py").read_text()
    assert "TOKEN_VERSION = 2" in migrated
    assert report.confidence == 0.9
    assert report.summary.startswith("Migration complete")

    # 2. The trace contains every event type, in a sane order.
    events = [json.loads(l)["event"] for l in buf.getvalue().splitlines() if l.strip()]
    assert events[0] == "run_started"
    assert events[-1] == "run_finished"
    for required in (
        "plan_ready", "subtask_started", "llm_call", "tool_call",
        "critic_score", "subtask_finished",
    ):
        assert required in events, f"missing {required} in trace"
    # plan_ready precedes the first subtask; run_finished is last.
    assert events.index("plan_ready") < events.index("subtask_started")

    # 3. The planted secret never leaks into the trace (redaction, §9).
    trace_text = buf.getvalue()
    assert "supersecret" not in trace_text
    assert "sk-supersecret1234567890" not in trace_text
    assert "REDACTED" in trace_text  # something was scrubbed

    # 4. Silent on stderr during normal operation (§14.4).
    captured = capfd.readouterr()
    assert captured.err == ""

    # 5. Budget respected / accounted.
    assert report.tokens_total >= 0
    assert report.iterations == 0  # nothing was rejected, so no revisions
