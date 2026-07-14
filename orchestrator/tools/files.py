"""File tools with workspace containment (SPEC §7 file tools).

Paths are resolved with symlink- and ``..``-awareness, then checked for
containment inside the workspace. Tool failures return *error strings* (not
exceptions) so the worker loop can decide what to do (§11 "Tool execution
fails -> return the error string to the worker").

READ ROOTS: reads (``read_file``/``list_files``) may additionally resolve
into explicitly configured extra directories (``Config.read_roots``) via
``..`` or absolute paths — e.g. a sibling project the task references.
WRITES never leave the workspace regardless of roots; mutation stays
confined. Default is no roots — behavior unchanged.

NOTE (§7.3): this is a speed bump, not a security boundary. The real boundary
is the container the orchestrator runs inside.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from ..constants import MAX_FILE_READ_BYTES


class WorkspaceEscape(Exception):
    """Raised internally when a path resolves outside the allowed area."""


def _contains(root: Path, resolved: Path) -> bool:
    return resolved == root or root in resolved.parents


def resolve_in_workspace(workspace: str | os.PathLike, path: str) -> Path:
    """Resolve ``path`` (relative to workspace) and confirm containment.

    Uses ``os.path.realpath`` so symlinks and ``..`` are collapsed before the
    containment check — a symlink pointing outside the workspace is rejected.
    This is the WRITE rule: read roots never apply here.
    """
    ws = Path(os.path.realpath(workspace))
    # Reject absolute paths outright; everything is relative to the workspace.
    candidate = (ws / path)
    resolved = Path(os.path.realpath(candidate))
    if not _contains(ws, resolved):
        raise WorkspaceEscape(f"path escapes workspace: {path!r}")
    return resolved


def resolve_readable(
    workspace: str | os.PathLike, path: str, read_roots: Sequence[str] = ()
) -> Path:
    """Resolve ``path`` for READ access: inside the workspace, or inside one
    of the configured read roots (reachable via ``..`` or absolute paths).

    Same symlink discipline as writes: the REAL resolved location must land
    in an allowed area — a symlink inside the workspace pointing somewhere
    un-rooted is still rejected.
    """
    ws = Path(os.path.realpath(workspace))
    p = Path(path)
    candidate = p if p.is_absolute() else ws / p
    resolved = Path(os.path.realpath(candidate))
    if _contains(ws, resolved):
        return resolved
    for root in read_roots:
        if _contains(Path(os.path.realpath(root)), resolved):
            return resolved
    hint = " (no read roots configured)" if not read_roots else ""
    raise WorkspaceEscape(f"path escapes workspace and read roots: {path!r}{hint}")


def read_file(
    workspace: str,
    path: str,
    max_bytes: int = MAX_FILE_READ_BYTES,
    read_roots: Sequence[str] = (),
) -> str:
    try:
        resolved = resolve_readable(workspace, path, read_roots)
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


def list_files(workspace: str, path: str = ".", read_roots: Sequence[str] = ()) -> str:
    try:
        resolved = resolve_readable(workspace, path, read_roots)
    except WorkspaceEscape as e:
        return f"ERROR: {e}"
    if not resolved.exists():
        return f"ERROR: path not found: {path}"
    if resolved.is_file():
        return path
    ws = Path(os.path.realpath(workspace))
    entries = []
    for child in sorted(resolved.iterdir()):
        try:
            name = str(child.relative_to(ws))  # workspace-relative when possible
        except ValueError:
            name = str(child)  # read-root listing: absolute, unambiguous
        entries.append(f"{name}/" if child.is_dir() else name)
    return "\n".join(entries) if entries else "(empty)"
