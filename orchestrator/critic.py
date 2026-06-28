"""The critic loop (SPEC §6).

The critic gates each result *before* it becomes a dependency input or reaches
the synthesizer (§2, §6). Two v2 behaviours are load-bearing and tested:

* **Accepted revisions are kept** (§6 fix): ``worker_result = revised`` happens
  unconditionally for every revision actually scored, so the result the critic
  approved is the one that propagates — never the stale rejected version.
* **Convergence guard** (§6.2): a field-identical revision, or a score that
  fails to improve, stops the loop early.
"""

from __future__ import annotations

import json
from typing import Awaitable, Callable

from .budget import Budget
from .config import Config
from .json_extract import extract_json
from .llm import LLMClient
from .models import CriticScore, WorkerResult
from .observers import Observer, safe
from .roles import critic_prompt
from .worker import WorkerOutput

ReviseFn = Callable[[WorkerOutput, CriticScore], Awaitable[WorkerOutput]]


def is_accepted(score: CriticScore, threshold: float) -> bool:
    """The authoritative gate (§6.4): the critic's approval AND a score at or
    above threshold. A critic rejection cannot be rescued by threshold."""
    return score.approved and score.score >= threshold


def enforce_threshold(score: CriticScore, threshold: float) -> CriticScore:
    """Fold the threshold into ``approved`` so downstream checks are consistent
    (§6.4): a high score below threshold becomes unapproved; a rejection stays
    rejected."""
    approved = score.approved and score.score >= threshold
    return score.model_copy(update={"approved": approved})


def _no_progress(prev: CriticScore, new: CriticScore, prev_result: WorkerResult,
                 new_result: WorkerResult) -> bool:
    """Convergence guard (§6.2): identical result (value-aware) or non-improving
    score."""
    if new_result.model_dump() == prev_result.model_dump():
        return True
    return new.score <= prev.score


async def critic_score(
    llm: LLMClient,
    critic_role: str,
    worker_result: WorkerResult,
    original_task: str,
    config: Config,
    observer: Observer | None = None,
    budget: Budget | None = None,
    subtask_id: str = "",
) -> CriticScore:
    """Score a result against the per-role rubric (§6, §6.1). Invalid critic
    output defaults to a middling, unapproved score (§11)."""
    messages = [
        {"role": "system", "content": critic_prompt(critic_role), "_role": "critic"},
        {
            "role": "user",
            "content": (
                f"Original task:\n{original_task}\n\n"
                f"Result to score (JSON):\n{json.dumps(worker_result.model_dump(), default=str)}"
            ),
        },
    ]
    resp = await llm.complete(messages, tools=None, temperature=config.temperature_for("critic"))
    if budget is not None:
        budget.note_call(resp.usage.total_tokens)

    obj = extract_json(resp.content or "")
    if obj is None:
        score = CriticScore.failed_validation()
    else:
        try:
            score = CriticScore.model_validate(obj)
        except Exception:
            score = CriticScore.failed_validation()

    safe(
        observer, "critic_score",
        subtask_id=subtask_id, score=score.score, approved=score.approved,
        issues=score.issues, rubric=[r.model_dump() for r in score.rubric],
    )
    return score


async def run_critic_loop(
    llm: LLMClient,
    role: str,
    subtask_id: str,
    original_task: str,
    initial: WorkerOutput,
    revise_fn: ReviseFn,
    config: Config,
    observer: Observer | None = None,
    budget: Budget | None = None,
    critic_role: str | None = None,
) -> tuple[WorkerOutput, CriticScore]:
    """Run §6 exactly. Returns the best-available output (accepted or not) plus
    its score. ``revise_fn`` produces the next attempt while keeping context."""
    critic_role = critic_role or role
    threshold = config.threshold_for(role)

    current = initial
    score = enforce_threshold(
        await critic_score(llm, critic_role, current.result, original_task,
                           config, observer, budget, subtask_id),
        threshold,
    )

    i = 0
    while not is_accepted(score, threshold) and i < config.max_iterations_per_subtask:
        prev_result = current.result
        prev_score = score

        revised = await revise_fn(current, score)
        revised_score = enforce_threshold(
            await critic_score(llm, critic_role, revised.result, original_task,
                               config, observer, budget, subtask_id),
            threshold,
        )

        # v2 FIX: keep every revision we actually scored, so an accepted revision
        # is the one that propagates (never discarded).
        current = revised
        score = revised_score

        # Convergence guard (§6.2): identical revision or non-improving score.
        if _no_progress(prev_score, revised_score, prev_result, revised.result):
            break
        i += 1

    return current, score
