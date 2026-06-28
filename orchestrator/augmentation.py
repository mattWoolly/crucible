"""Plan augmentation (SPEC §5.2) — the single most important reliability feature.

After a plan is valid, ensure it is *complete*: always have a coder and a
synthesizer. Injected subtasks depend only on pre-existing ids, so acyclicity
is preserved. Augmentation is idempotent (§5.2).
"""

from __future__ import annotations

from .constants import CODER, RESEARCHER, SYNTHESIZER
from .models import Plan, Subtask


def _has_role(plan: Plan, role: str) -> bool:
    return any(s.role == role for s in plan.subtasks)


def _unique_id(plan: Plan, base: str) -> str:
    existing = {s.id for s in plan.subtasks}
    if base not in existing:
        return base
    i = 2
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"


def augment_plan(plan: Plan, original_task: str) -> Plan:
    """Return a complete plan: inject coder/synthesizer if missing. Idempotent."""
    subtasks = list(plan.subtasks)

    # 1. Ensure a coder exists.
    if not _has_role(Plan(subtasks=subtasks), CODER):
        researcher_ids = [s.id for s in subtasks if s.role == RESEARCHER]
        deps = researcher_ids or [s.id for s in subtasks]
        coder = Subtask(
            id=_unique_id(Plan(subtasks=subtasks), "auto_coder"),
            role=CODER,
            task=(
                "Complete the original task using prior worker outputs.\n"
                f"Original task: {original_task}"
            ),
            depends_on=deps,
            inputs="all prior worker outputs",
        )
        subtasks.append(coder)

    # 2. Ensure a synthesizer exists — depends on ALL existing subtasks.
    if not _has_role(Plan(subtasks=subtasks), SYNTHESIZER):
        synth = Subtask(
            id=_unique_id(Plan(subtasks=subtasks), "auto_synth"),
            role=SYNTHESIZER,
            task="Merge all worker outputs into a final answer.",
            depends_on=[s.id for s in subtasks],
            inputs="every worker output",
        )
        subtasks.append(synth)

    return Plan(reasoning=plan.reasoning, subtasks=subtasks)
