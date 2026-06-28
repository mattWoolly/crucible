"""Plan validation and topological layering (SPEC §5.1).

Before augmentation, reject/repair plans that cannot execute. Validation
failures raise ``PlanValidationError`` with a message naming the specific
defect so it can be fed back to the planner as a corrective prompt (§5.1).
"""

from __future__ import annotations

from .constants import ALLOWED_PLAN_ROLES
from .errors import PlanValidationError
from .models import Plan, Subtask


def _index(subtasks: list[Subtask]) -> dict[str, Subtask]:
    return {s.id: s for s in subtasks}


def topological_layers(subtasks: list[Subtask]) -> list[list[str]]:
    """Kahn-style layering: each layer is the set of nodes whose deps are all
    already placed. Raises ``PlanValidationError`` naming a cycle if one exists.
    """
    by_id = _index(subtasks)
    remaining = {s.id: set(s.depends_on) for s in subtasks}
    placed: set[str] = set()
    layers: list[list[str]] = []

    while remaining:
        ready = sorted(sid for sid, deps in remaining.items() if deps <= placed)
        if not ready:
            # Everything left is in a cycle; surface a representative chain.
            cycle = _find_cycle({sid: by_id[sid].depends_on for sid in remaining})
            raise PlanValidationError(f"cycle: {' -> '.join(cycle)}")
        layers.append(ready)
        for sid in ready:
            placed.add(sid)
            del remaining[sid]
    return layers


def _find_cycle(graph: dict[str, list[str]]) -> list[str]:
    """Return a node chain that forms a cycle (best-effort, for the message)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}
    stack: list[str] = []

    def dfs(n: str) -> list[str] | None:
        color[n] = GRAY
        stack.append(n)
        for m in graph.get(n, []):
            if m not in color:
                continue
            if color[m] == GRAY:
                return stack[stack.index(m):] + [m]
            if color[m] == WHITE:
                r = dfs(m)
                if r:
                    return r
        stack.pop()
        color[n] = BLACK
        return None

    for n in graph:
        if color[n] == WHITE:
            r = dfs(n)
            if r:
                return r
    return list(graph)  # fallback


def validate_plan(plan: Plan) -> list[list[str]]:
    """Validate schema + DAG and return the topological layers (§5.1).

    Checks, in order: unique ids, allowed roles, no dangling deps, acyclic
    (layering). Raises ``PlanValidationError`` naming the first defect found.
    """
    subtasks = plan.subtasks
    if not subtasks:
        raise PlanValidationError("plan has no subtasks")

    # 1. Unique ids.
    seen: set[str] = set()
    for s in subtasks:
        if s.id in seen:
            raise PlanValidationError(f"duplicate subtask id: {s.id!r}")
        seen.add(s.id)

    # 1b. Allowed roles.
    for s in subtasks:
        if s.role not in ALLOWED_PLAN_ROLES:
            raise PlanValidationError(
                f"subtask {s.id!r} has role {s.role!r} not in {sorted(ALLOWED_PLAN_ROLES)}"
            )

    # 2. No dangling dependencies.
    for s in subtasks:
        for dep in s.depends_on:
            if dep not in seen:
                raise PlanValidationError(
                    f"subtask {s.id!r} depends on {dep!r} which does not exist"
                )
        if s.id in s.depends_on:
            raise PlanValidationError(f"subtask {s.id!r} depends on itself")

    # 3 + 4. Acyclic + layerable.
    return topological_layers(subtasks)
