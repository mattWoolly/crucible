"""Synthesis + final-answer verification (SPEC §2 steps 4-5, §6.3).

The synthesizer merges the APPROVED worker outputs into the answer the user
receives. That merged answer then gets its **own** critic pass with the
synthesizer rubric (§6.3) — in v1 only worker outputs were gated and the merge
shipped unverified. If the final critic rejects, the synthesizer iterates
(same loop, same ``max_iterations``).
"""

from __future__ import annotations

from .budget import Budget
from .config import Config
from .constants import SYNTHESIZER
from .critic import run_critic_loop
from .llm import LLMClient
from .models import CriticScore, Subtask, WorkerResult
from .observers import Observer
from .worker import WorkerOutput, revise_worker, run_worker


async def run_synthesis(
    llm: LLMClient,
    synth_subtask: Subtask,
    approved_results: dict[str, WorkerResult],
    original_task: str,
    config: Config,
    observer: Observer | None = None,
    budget: Budget | None = None,
) -> tuple[WorkerOutput, CriticScore]:
    """Run the synthesizer over approved results, then gate the merged answer
    with a final critic pass that re-synthesizes on rejection (§6.3)."""
    initial = await run_worker(
        llm, SYNTHESIZER, synth_subtask, approved_results, config, observer, budget
    )

    async def revise_fn(prev: WorkerOutput, score: CriticScore) -> WorkerOutput:
        return await revise_worker(
            llm, SYNTHESIZER, synth_subtask, prev.messages,
            score.issues, score.suggestions, config, observer, budget,
        )

    final_output, final_score = await run_critic_loop(
        llm, SYNTHESIZER, synth_subtask.id, original_task,
        initial, revise_fn, config, observer, budget, critic_role=SYNTHESIZER,
    )
    return final_output, final_score
