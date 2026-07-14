"""Read roots: read-only access outside the workspace (tools/files.py,
registry, worker orientation, operator). Writes stay confined — always."""

import os

from orchestrator.config import Config
from orchestrator.llm import FakeLLMClient, ToolCall, llm_response
from orchestrator.models import Subtask
from orchestrator.operator import OPERATOR_ROLE, Operator
from orchestrator.tools import files
from orchestrator.tools.registry import execute_tool
from orchestrator.worker import run_worker, workspace_orientation


def setup_tree(tmp_path):
    """tmp/ ├── ws/            (workspace)
            ├── sibling/lib.py (a read root)
            └── secret/key.txt (NOT a root)"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (sibling / "lib.py").write_text("def shared(): pass")
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "key.txt").write_text("hunter2")
    return str(ws), str(sibling), str(secret)


# -- files.py semantics -----------------------------------------------------------


def test_default_behavior_unchanged_dotdot_rejected(tmp_path):
    ws, _, _ = setup_tree(tmp_path)
    out = files.read_file(ws, "../sibling/lib.py")
    assert out.startswith("ERROR:") and "no read roots configured" in out


def test_relative_dotdot_into_root_allowed(tmp_path):
    ws, sibling, _ = setup_tree(tmp_path)
    out = files.read_file(ws, "../sibling/lib.py", read_roots=(sibling,))
    assert out == "def shared(): pass"


def test_absolute_path_into_root_allowed(tmp_path):
    ws, sibling, _ = setup_tree(tmp_path)
    out = files.read_file(ws, os.path.join(sibling, "lib.py"), read_roots=(sibling,))
    assert out == "def shared(): pass"


def test_paths_outside_all_roots_still_rejected(tmp_path):
    ws, sibling, secret = setup_tree(tmp_path)
    out = files.read_file(ws, os.path.join(secret, "key.txt"), read_roots=(sibling,))
    assert out.startswith("ERROR:") and "escapes" in out
    # .. climbing above the root is rejected too
    out2 = files.read_file(ws, "../secret/key.txt", read_roots=(sibling,))
    assert out2.startswith("ERROR:")


def test_sibling_name_prefix_does_not_leak(tmp_path):
    # root "sibling" must not grant "sibling-2"
    ws, sibling, _ = setup_tree(tmp_path)
    evil = tmp_path / "sibling-2"
    evil.mkdir()
    (evil / "x.txt").write_text("nope")
    out = files.read_file(ws, "../sibling-2/x.txt", read_roots=(sibling,))
    assert out.startswith("ERROR:")


def test_symlink_out_of_root_rejected(tmp_path):
    ws, sibling, secret = setup_tree(tmp_path)
    os.symlink(os.path.join(secret, "key.txt"), os.path.join(sibling, "link.txt"))
    out = files.read_file(ws, "../sibling/link.txt", read_roots=(sibling,))
    assert out.startswith("ERROR:")  # realpath lands in secret/ -> denied


def test_writes_never_use_read_roots(tmp_path):
    ws, sibling, _ = setup_tree(tmp_path)
    out = files.write_file(ws, "../sibling/pwned.txt", "x")
    assert out.startswith("ERROR:") and "escapes workspace" in out
    assert not (tmp_path / "sibling" / "pwned.txt").exists()


def test_list_files_in_root_uses_absolute_names(tmp_path):
    ws, sibling, _ = setup_tree(tmp_path)
    out = files.list_files(ws, "../sibling", read_roots=(sibling,))
    assert os.path.join(sibling, "lib.py") in out


# -- registry + worker plumbing ------------------------------------------------------


async def test_execute_tool_passes_read_roots(tmp_path):
    ws, sibling, _ = setup_tree(tmp_path)
    ok = await execute_tool("read_file", {"path": "../sibling/lib.py"}, ws, (sibling,))
    assert ok.error is None and "shared" in ok.content
    denied = await execute_tool("read_file", {"path": "../sibling/lib.py"}, ws)
    assert denied.error is not None
    write = await execute_tool(
        "write_file", {"path": "../sibling/no.txt", "content": "x"}, ws, (sibling,)
    )
    assert write.error is not None  # roots never widen writes


def test_orientation_mentions_roots_only_when_configured(tmp_path):
    ws, sibling, _ = setup_tree(tmp_path)
    plain = workspace_orientation(ws)
    assert "Absolute paths and `..` are rejected." in plain
    rooted = workspace_orientation(ws, read_roots=(sibling,))
    assert sibling in rooted and "READ" in rooted
    assert "rejected for WRITES" in rooted


async def test_worker_reads_root_via_config(tmp_path):
    ws, sibling, _ = setup_tree(tmp_path)
    llm = FakeLLMClient({"coder": [
        llm_response(tool_calls=[ToolCall(id="t1", name="read_file",
                                          args={"path": "../sibling/lib.py"})]),
        llm_response('{"summary": "read it", "confidence": 0.9}'),
    ]})
    cfg = Config(workspace=ws, read_roots=(sibling,))
    out = await run_worker(llm, "coder", Subtask(id="c1", role="coder", task="t"), {}, cfg)
    tool_msg = next(m for m in out.messages if m.get("role") == "tool")
    assert "def shared" in tool_msg["content"]


# -- operator ---------------------------------------------------------------------


async def test_operator_reads_roots_and_live_mutation(tmp_path):
    ws, sibling, _ = setup_tree(tmp_path)
    roots: list[str] = []  # starts empty — shared list, mutated later like /roots add

    async def runner(task):
        raise AssertionError("no dispatch expected")

    op = Operator(
        llm=FakeLLMClient({OPERATOR_ROLE: [
            llm_response(tool_calls=[ToolCall(id="r1", name="read_file",
                                              args={"path": "../sibling/lib.py"})]),
            llm_response("denied as expected"),
            llm_response(tool_calls=[ToolCall(id="r2", name="read_file",
                                              args={"path": "../sibling/lib.py"})]),
            llm_response("read it"),
        ]}),
        workspace=ws,
        swarm_runner=runner,
        read_roots=roots,
    )
    await op.chat("try to read the sibling")
    first_result = [m for m in op.messages if m.get("role") == "tool"][0]
    assert first_result["content"].startswith("ERROR:")

    roots.append(sibling)  # /roots add — applies mid-session
    await op.chat("try again")
    last_result = [m for m in op.messages if m.get("role") == "tool"][-1]
    assert "def shared" in last_result["content"]
