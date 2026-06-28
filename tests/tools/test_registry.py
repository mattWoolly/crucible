import pytest

from orchestrator.constants import CODER, RESEARCHER, SYNTHESIZER
from orchestrator.tools.registry import execute_tool, tool_defs_for, tools_for


def test_researcher_has_read_not_write():
    t = tools_for(RESEARCHER)
    assert "read_file" in t and "list_files" in t
    assert "write_file" not in t and "run_shell" not in t


def test_coder_has_all_tools():
    t = tools_for(CODER)
    assert t == {"read_file", "list_files", "write_file", "run_shell"}


def test_toolless_role_has_no_defs():
    assert tool_defs_for(SYNTHESIZER) == []


async def test_execute_read_file(tmp_path):
    (tmp_path / "x.txt").write_text("data")
    res = await execute_tool("read_file", {"path": "x.txt"}, str(tmp_path))
    assert res.content == "data"
    assert res.error is None


async def test_execute_unknown_tool(tmp_path):
    res = await execute_tool("frobnicate", {}, str(tmp_path))
    assert res.error is not None and "unknown tool" in res.content


async def test_execute_surfaces_tool_error(tmp_path):
    res = await execute_tool("read_file", {"path": "../escape"}, str(tmp_path))
    assert res.error is not None
