"""Slash-command parsing for the TUI (pure — no Textual imports).

Input starting with ``/`` is a command; anything else is a task for the
orchestrator. Parsing is separated from execution so it is trivially
hermetic to test; the app maps parsed commands onto its own methods.
"""

from __future__ import annotations

import difflib
import shlex
from dataclasses import dataclass, field


class CommandError(Exception):
    """Bad command input; the message is shown verbatim in the transcript."""


@dataclass(frozen=True)
class SlashCommand:
    name: str
    args: list[str] = field(default_factory=list)


# name -> (usage, one-line help). The single source for /help and validation.
COMMANDS: dict[str, tuple[str, str]] = {
    "help": ("/help", "show this command list"),
    "permissions": (
        "/permissions [auto|ask|readonly] | allow <tool> | reset",
        "show or set the tool permission mode / session allowlist",
    ),
    "theme": ("/theme [name]", "list themes or switch to one"),
    "model": (
        "/model [provider] [model]",
        "show providers/keys, switch provider, or set the swarm model",
    ),
    "rep": (
        "/rep [provider] [model]",
        "show or set the operator's (your representative's) model",
    ),
    "swarm": ("/swarm <task>", "dispatch the swarm directly, bypassing the operator"),
    "reset": ("/reset", "start a fresh operator conversation (session forgotten)"),
    "tokens": ("/tokens", "token / call / budget utilization for this session"),
    "plan": ("/plan", "re-show the current run's plan"),
    "copy": (
        "/copy [reply|report|all]",
        "copy the last rep reply / final report / whole transcript to the clipboard",
    ),
    "clear": ("/clear", "clear the transcript"),
    "workspace": ("/workspace [dir]", "show or change the sandboxed workspace"),
    "roots": (
        "/roots [add <dir> | rm <dir> | clear]",
        "read-only paths agents may access OUTSIDE the workspace (e.g. ../sibling)",
    ),
    "threshold": ("/threshold [0-10]", "show or set the critic approval threshold"),
    "quit": ("/quit", "exit crucible"),
}

_ALIASES = {"exit": "quit", "q": "quit", "perms": "permissions", "?": "help"}


def parse_input(text: str) -> SlashCommand | None:
    """Parse one line of user input.

    Returns ``None`` for plain task text, a ``SlashCommand`` for ``/...``.
    Raises ``CommandError`` on an unknown or malformed command.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    try:
        tokens = shlex.split(stripped[1:])
    except ValueError as e:
        raise CommandError(f"could not parse command: {e}") from e
    if not tokens:
        raise CommandError("empty command — try /help")
    name = _ALIASES.get(tokens[0].lower(), tokens[0].lower())
    if name not in COMMANDS:
        hint = difflib.get_close_matches(name, COMMANDS, n=1)
        suffix = f" — did you mean /{hint[0]}?" if hint else " — try /help"
        raise CommandError(f"unknown command /{tokens[0]}{suffix}")
    return SlashCommand(name=name, args=tokens[1:])


def help_lines() -> list[str]:
    """``(usage, help)`` rows, aligned for the transcript."""
    width = max(len(usage) for usage, _ in COMMANDS.values())
    return [f"{usage:<{width}}  {desc}" for usage, desc in COMMANDS.values()]
