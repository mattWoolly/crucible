"""Interactive permission policy for engine tool calls (pure asyncio,
no Textual imports — the app supplies the ``asker`` UI callback).

Modes, in decreasing trust:

  auto      every tool call runs (headless-equivalent; sandbox still applies)
  ask       mutating tools (write_file, run_shell) prompt the user; read-only
            tools run without asking
  readonly  mutating tools are denied outright; read-only tools run

``PermissionGate.check`` is the coroutine handed to ``Config.tool_gate``.
Prompts are serialized so parallel workers can't stack modals.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

MODES = ("auto", "ask", "readonly")

READ_ONLY_TOOLS = frozenset({"read_file", "list_files"})
MUTATING_TOOLS = frozenset({"write_file", "run_shell"})

# The asker returns one of these:
ALLOW_ONCE = "allow"
ALLOW_ALWAYS = "always"  # allow + add tool to the session allowlist
DENY = "deny"

Asker = Callable[[str, dict], Awaitable[str]]


class PermissionGate:
    def __init__(self, asker: Asker | None = None, mode: str = "ask") -> None:
        self._asker = asker
        self._mode = "ask"
        self.mode = mode
        self.session_allowed: set[str] = set()
        self._prompt_lock = asyncio.Lock()

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        if value not in MODES:
            raise ValueError(f"unknown permission mode {value!r} (use {'/'.join(MODES)})")
        self._mode = value

    def allow_always(self, tool: str) -> None:
        self.session_allowed.add(tool)

    def reset(self) -> None:
        self.session_allowed.clear()

    async def check(self, name: str, args: dict) -> tuple[bool, str | None]:
        """The ``Config.tool_gate`` coroutine: (allowed, reason-if-denied)."""
        if self._mode == "auto" or name in READ_ONLY_TOOLS:
            return True, None
        if name in self.session_allowed:
            return True, None
        if self._mode == "readonly":
            return False, f"read-only mode — {name} is not permitted"
        # mode == "ask": unknown tools are treated like mutating ones.
        if self._asker is None:
            return False, f"no interactive prompt available to approve {name}"
        async with self._prompt_lock:
            if name in self.session_allowed:  # approved while we waited
                return True, None
            answer = await self._asker(name, args)
        if answer == ALLOW_ALWAYS:
            self.allow_always(name)
            return True, None
        if answer == ALLOW_ONCE:
            return True, None
        return False, f"user denied {name}"
