"""The Operator agent (orchestrator/operator.py) — hermetic (FakeLLMClient)."""


from orchestrator.llm import FakeLLMClient, ToolCall, llm_response
from orchestrator.models import FinalReport, WorkerResult
from orchestrator.operator import OPERATOR_ROLE, Operator, summarize_report


def report(verified=True, summary="built it") -> FinalReport:
    return FinalReport(
        summary=summary, confidence=0.9,
        subtask_results={"code_impl": WorkerResult(summary="wrote code", confidence=0.9)},
        critic_scores={}, iterations=1, tokens_total=12345, verified=verified,
    )


def make_operator(tmp_path, scripts, runner=None, **kw) -> Operator:
    async def default_runner(task):
        return report()

    return Operator(
        llm=FakeLLMClient(scripts),
        workspace=str(tmp_path),
        swarm_runner=runner or default_runner,
        **kw,
    )


def swarm_call(task="build the thing", cid="c1"):
    return llm_response(tool_calls=[ToolCall(id=cid, name="run_swarm", args={"task": task})])


# -- conversation ---------------------------------------------------------------


async def test_plain_conversation_no_tools(tmp_path):
    op = make_operator(tmp_path, {OPERATOR_ROLE: [llm_response("hi! what shall we build?")]})
    reply = await op.chat("hello")
    assert reply == "hi! what shall we build?"
    # session recorded both sides
    assert op.messages[-2]["content"] == "hello"
    assert op.messages[-1] == {"role": "assistant", "content": "hi! what shall we build?"}


async def test_session_persists_across_turns(tmp_path):
    op = make_operator(tmp_path, {OPERATOR_ROLE: [llm_response("a"), llm_response("b")]})
    await op.chat("one")
    await op.chat("two")
    roles = [m["role"] for m in op.messages]
    assert roles == ["system", "user", "assistant", "user", "assistant"]


async def test_reset_keeps_only_system_prompt(tmp_path):
    op = make_operator(tmp_path, {OPERATOR_ROLE: [llm_response("a")]})
    await op.chat("one")
    op.reset()
    assert len(op.messages) == 1 and op.messages[0]["role"] == "system"


# -- dispatching the swarm --------------------------------------------------------


async def test_run_swarm_dispatches_and_report_feeds_back(tmp_path):
    briefs: list[str] = []

    async def runner(task):
        briefs.append(task)
        return report(verified=True, summary="migration done")

    op = make_operator(
        tmp_path,
        {OPERATOR_ROLE: [swarm_call("migrate tokens v1 -> v2; ruff+pytest green"),
                         llm_response("Done — verified green.")]},
        runner=runner,
    )
    reply = await op.chat("migrate the tokens please")
    assert briefs == ["migrate tokens v1 -> v2; ruff+pytest green"]
    assert reply == "Done — verified green."
    tool_msg = next(m for m in op.messages if m["role"] == "tool")
    assert "verify gate: PASS" in tool_msg["content"]
    assert "migration done" in tool_msg["content"]


async def test_on_dispatch_hook_sees_the_brief(tmp_path):
    seen: list[str] = []
    op = make_operator(
        tmp_path,
        {OPERATOR_ROLE: [swarm_call("the brief"), llm_response("ok")]},
    )
    op.on_dispatch = seen.append
    await op.chat("go")
    assert seen == ["the brief"]


async def test_swarm_failure_reported_not_raised(tmp_path):
    async def runner(task):
        raise RuntimeError("provider exploded")

    op = make_operator(
        tmp_path,
        {OPERATOR_ROLE: [swarm_call(), llm_response("the run failed: provider exploded")]},
        runner=runner,
    )
    reply = await op.chat("go")
    tool_msg = next(m for m in op.messages if m["role"] == "tool")
    assert "ERROR: swarm run failed: provider exploded" in tool_msg["content"]
    assert "failed" in reply


async def test_empty_brief_rejected(tmp_path):
    op = make_operator(
        tmp_path,
        {OPERATOR_ROLE: [swarm_call(task="  "), llm_response("sorry")]},
    )
    await op.chat("go")
    tool_msg = next(m for m in op.messages if m["role"] == "tool")
    assert "non-empty task brief" in tool_msg["content"]


async def test_gate_denial_blocks_dispatch(tmp_path):
    dispatched: list[str] = []

    async def runner(task):
        dispatched.append(task)
        return report()

    async def gate(name, args):
        return False, "user said no"

    op = make_operator(
        tmp_path,
        {OPERATOR_ROLE: [swarm_call(), llm_response("understood, not dispatching")]},
        runner=runner, tool_gate=gate,
    )
    await op.chat("go")
    assert dispatched == []
    tool_msg = next(m for m in op.messages if m["role"] == "tool")
    assert "denied by permission policy" in tool_msg["content"]


# -- grounding tools ---------------------------------------------------------------


async def test_read_and_list_files_ground_the_operator(tmp_path):
    (tmp_path / "PROJECT.md").write_text("the spec")
    op = make_operator(
        tmp_path,
        {OPERATOR_ROLE: [
            llm_response(tool_calls=[
                ToolCall(id="l1", name="list_files", args={}),
                ToolCall(id="r1", name="read_file", args={"path": "PROJECT.md"}),
            ]),
            llm_response("the project has a spec"),
        ]},
    )
    reply = await op.chat("what's in the workspace?")
    tool_msgs = [m for m in op.messages if m["role"] == "tool"]
    assert any("PROJECT.md" in m["content"] for m in tool_msgs)
    assert any("the spec" in m["content"] for m in tool_msgs)
    assert reply == "the project has a spec"


# -- resilience --------------------------------------------------------------------


async def test_dangling_tool_call_healed_after_cancel(tmp_path):
    """A cancelled turn can leave assistant tool_calls without results; the
    next chat() must backfill them so the session stays replayable."""
    op = make_operator(tmp_path, {OPERATOR_ROLE: [llm_response("recovered")]})
    op.messages.append({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "t1", "type": "function",
                        "function": {"name": "run_swarm", "arguments": "{}"}}],
    })
    reply = await op.chat("hello again")
    assert reply == "recovered"
    healed = [m for m in op.messages if m.get("role") == "tool"]
    assert healed and "cancelled" in healed[0]["content"]
    # tool results come BEFORE the new user message
    assert op.messages.index(healed[0]) < len(op.messages) - 2


async def test_step_budget_bounds_the_loop(tmp_path):
    # model calls tools forever; loop must stop at max_steps
    endless = llm_response(tool_calls=[ToolCall(id="x", name="list_files", args={})])
    op = make_operator(tmp_path, {OPERATOR_ROLE: [endless] * 50}, max_steps=3)
    reply = await op.chat("loop forever")
    assert "step budget" in reply
    llm_calls = sum(1 for m in op.messages if m.get("role") == "assistant")
    assert llm_calls <= 4


def test_summarize_report_verify_first():
    s = summarize_report(report(verified=False, summary="tests failing"))
    assert s.startswith("verify gate: FAIL")
    assert "tests failing" in s and "code_impl" in s
