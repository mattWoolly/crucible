"""File tools with workspace containment (SPEC §7 file tools).

Paths are resolved with symlink- and ``..``-awareness, then checked for
containment inside the workspace. Tool failures return *error strings* (not
exceptions) so the worker loop can decide what to do (§11 "Tool execution
fails -> return the error string to the worker").

NOTE (§7.3): this is a speed bump, not a security boundary. The real boundary
is the container the orchestrator runs inside.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..constants import MAX_FILE_READ_BYTES


class WorkspaceEscape(Exception):
    """Raised internally when a path resolves outside the workspace."""


def resolve_in_workspace(workspace: str | os.PathLike, path: str) -> Path:
    """Resolve ``path`` (relative to workspace) and confirm containment.

    Uses ``os.path.realpath`` so symlinks and ``..`` are collapsed before the
    containment check — a symlink pointing outside the workspace is rejected.
    """
    ws = Path(os.path.realpath(workspace))
    # Reject absolute paths outright; everything is relative to the workspace.
    candidate = (ws / path)
    resolved = Path(os.path.realpath(candidate))
    if resolved != ws and ws not in resolved.parents:
        raise WorkspaceEscape(f"path escapes workspace: {path!r}")
    return resolved


def read_file(workspace: str, path: str, max_bytes: int = MAX_FILE_READ_BYTES) -> str:
    try:
        resolved = resolve_in_workspace(workspace, path)
    except WorkspaceEscape as e:
        return f"ERROR: {e}"
    if not resolved.exists():
        return f"ERROR: file not found: {path}"
    if resolved.is_dir():
        return f"ERROR: is a directory: {path}"
    data = resolved.read_bytes()
    if len(data) > max_bytes:
        text = data[:max_bytes].decode("utf-8", errors="replace")
        return f"{text}\n\n[TRUNCATED: file exceeds {max_bytes} bytes]"
    return data.decode("utf-8", errors="replace")


def write_file(workspace: str, path: str, content: str) -> str:
    try:
        resolved = resolve_in_workspace(workspace, path)
    except WorkspaceEscape as e:
        return f"ERROR: {e}"
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return f"OK: wrote {len(content)} bytes to {path}"


def list_files(workspace: str, path: str = ".") -> str:
    try:
        resolved = resolve_in_workspace(workspace, path)
    except WorkspaceEscape as e:
        return f"ERROR: {e}"
    if not resolved.exists():
        return f"ERROR: path not found: {path}"
    if resolved.is_file():
        return path
    ws = Path(os.path.realpath(workspace))
    entries = []
    for child in sorted(resolved.iterdir()):
        rel = child.relative_to(ws)
        entries.append(f"{rel}/" if child.is_dir() else str(rel))
    return "\n".join(entries) if entries else "(empty)"
