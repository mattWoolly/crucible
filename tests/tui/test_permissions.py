"""PermissionGate semantics (orchestrator/tui/permissions.py) — pure asyncio."""

import asyncio

import pytest

from orchestrator.tui.permissions import (
    ALLOW_ALWAYS,
    ALLOW_ONCE,
    DENY,
    PermissionGate,
)


def make_asker(answers: list[str], calls: list[tuple[str, dict]] | None = None):
    async def asker(name: str, args: dict) -> str:
        if calls is not None:
            calls.append((name, args))
        return answers.pop(0)

    return asker


async def test_auto_mode_allows_everything_without_asking():
    calls: list = []
    gate = PermissionGate(asker=make_asker([], calls), mode="auto")
    for tool in ("read_file", "write_file", "run_shell"):
        allowed, reason = await gate.check(tool, {})
        assert allowed and reason is None
    assert calls == []  # never prompted


async def test_readonly_mode_denies_mutating_allows_reads():
    gate = PermissionGate(asker=None, mode="readonly")
    assert (await gate.check("read_file", {"path": "x"}))[0] is True
    assert (await gate.check("list_files", {}))[0] is True
    allowed, reason = await gate.check("write_file", {"path": "x", "content": ""})
    assert allowed is False and "read-only" in reason
    allowed, reason = await gate.check("run_shell", {"command": "pytest"})
    assert allowed is False


async def test_ask_mode_reads_never_prompt():
    calls: list = []
    gate = PermissionGate(asker=make_asker([], calls), mode="ask")
    assert (await gate.check("read_file", {}))[0] is True
    assert calls == []


async def test_ask_mode_prompts_for_mutating_and_honors_deny():
    calls: list = []
    gate = PermissionGate(asker=make_asker([DENY], calls), mode="ask")
    allowed, reason = await gate.check("run_shell", {"command": "rm -rf ."})
    assert allowed is False and "denied" in reason
    assert calls == [("run_shell", {"command": "rm -rf ."})]


async def test_allow_once_does_not_persist():
    gate = PermissionGate(asker=make_asker([ALLOW_ONCE, DENY]), mode="ask")
    assert (await gate.check("write_file", {}))[0] is True
    assert (await gate.check("write_file", {}))[0] is False  # asked again


async def test_allow_always_persists_for_the_session():
    calls: list = []
    gate = PermissionGate(asker=make_asker([ALLOW_ALWAYS], calls), mode="ask")
    assert (await gate.check("run_shell", {}))[0] is True
    assert (await gate.check("run_shell", {}))[0] is True
    assert len(calls) == 1  # only the first call prompted
    assert "run_shell" in gate.session_allowed


async def test_reset_clears_session_allowlist():
    gate = PermissionGate(asker=make_asker([ALLOW_ALWAYS, DENY]), mode="ask")
    await gate.check("run_shell", {})
    gate.reset()
    assert (await gate.check("run_shell", {}))[0] is False


async def test_ask_mode_without_asker_denies_mutating():
    gate = PermissionGate(asker=None, mode="ask")
    allowed, reason = await gate.check("write_file", {})
    assert allowed is False and "write_file" in reason


async def test_unknown_tool_is_treated_as_mutating():
    gate = PermissionGate(asker=make_asker([DENY]), mode="ask")
    assert (await gate.check("mystery_tool", {}))[0] is False


async def test_concurrent_asks_serialize_and_reuse_always_answer():
    """Two parallel workers hit the same tool: one modal, second ride free."""
    started = asyncio.Event()

    async def slow_asker(name: str, args: dict) -> str:
        started.set()
        await asyncio.sleep(0.01)
        return ALLOW_ALWAYS

    gate = PermissionGate(asker=slow_asker, mode="ask")
    a, b = await asyncio.gather(gate.check("run_shell", {}), gate.check("run_shell", {}))
    assert a[0] is True and b[0] is True


def test_invalid_mode_rejected():
    gate = PermissionGate(mode="ask")
    with pytest.raises(ValueError, match="unknown permission mode"):
        gate.mode = "yolo"
