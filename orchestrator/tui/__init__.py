"""Full-screen terminal UI for Crucible (Textual).

Launch with ``uv run crucible [--workspace DIR]``. The TUI is a *client* of
the engine: it observes via the standard Observer interface and controls tool
execution via ``Config.tool_gate`` — no engine internals are reached around.
"""

from .app import CrucibleApp, main

__all__ = ["CrucibleApp", "main"]
