"""Tool registry: OpenAI tool schemas, per-role tool sets, dispatch (SPEC §3, §4, §7).

Roles differ only in their registered tools (§3): researcher reads, coder also
writes and runs shell, everyone else has none.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..constants import CODER, RESEARCHER
from . import files, shell

# --- Tool JSON schemas (OpenAI function-tool format) -----------------------
_READ_FILE = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a UTF-8 text file inside the workspace. Use a relative path.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}
_LIST_FILES = {
    "type": "function",
    "function": {
        "name": "list_files",
        "description": "List files/directories at a relative path inside the workspace.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": [],
        },
    },
}
_WRITE_FILE = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write/overwrite a UTF-8 text file inside the workspace (relative path).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
}
_RUN_SHELL = {
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": (
            "Run an allowlisted command (no shell, no metacharacters, relative "
            "paths only). Use to run tests/linters, e.g. 'pytest -q'."
        ),
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}

_ROLE_TOOLS: dict[str, list[dict]] = {
    RESEARCHER: [_READ_FILE, _LIST_FILES],
    CODER: [_READ_FILE, _LIST_FILES, _WRITE_FILE, _RUN_SHELL],
}


def tool_defs_for(role: str) -> list[dict]:
    """OpenAI tool schemas registered for a role (empty for tool-less roles)."""
    return list(_ROLE_TOOLS.get(role, []))


def tools_for(role: str) -> set[str]:
    return {t["function"]["name"] for t in _ROLE_TOOLS.get(role, [])}


@dataclass
class ToolResult:
    content: str
    error: str | None
    latency_ms: float = 0.0


async def execute_tool(name: str, args: dict, workspace: str) -> ToolResult:
    """Dispatch a tool call to its implementation, off the event loop thread.

    Returns a ``ToolResult``; tool-level failures are returned as error strings
    (§11) rather than raised.
    """
    loop = asyncio.get_event_loop()
    start = loop.time()

    def _run() -> str:
        if name == "read_file":
            return files.read_file(workspace, args.get("path", ""))
        if name == "list_files":
            return files.list_files(workspace, args.get("path", "."))
        if name == "write_file":
            return files.write_file(workspace, args.get("path", ""), args.get("content", ""))
        if name == "run_shell":
            return str(shell.run_shell(workspace, args.get("command", "")))
        return f"ERROR: unknown tool {name!r}"

    out = await asyncio.to_thread(_run)
    latency = (loop.time() - start) * 1000
    error = out if out.startswith("ERROR") else None
    return ToolResult(content=out, error=error, latency_ms=latency)
