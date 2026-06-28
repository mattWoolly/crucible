"""The orchestrator (SPEC §2, §11).

``run(task)`` drives the authoritative execution order (§2):

  1. plan -> validate -> augment
  2. for each topological layer, run its workers in parallel
  3. each worker result is critic-gated before it flows to dependents/synth
  4. the synthesizer runs once over approved results
  5. a final critic pass gates the synthesized answer; reject -> iterate synth

Global budgets bound the run (§10); recoverable failures degrade rather than
crash (§11); ``run_finished`` is always emitted and the observer flushed in
``finally`` (§9).
"""

from __future__ import annotations

import asyncio
import atexit

from .augmentation import augment_plan
from .budget import Budget
from .config import Config
from .constants import DEGRADED_CONFIDENCE, SYNTHESIZER
from .critic import run_critic_loop
from .errors import PlanValidationError
from .json_extract import extract_json
from .llm import LLMClient
from .models import CriticScore, FinalReport, Plan, Subtask, WorkerResult
from .observers import Observer, safe, select_observer
from .plan_validation import validate_plan
from .roles import system_prompt
from .synthesizer import run_synthesis
from .worker import WorkerOutput, revise_worker, run_worker


class Orchestrator:
    def __init__(self, llm: LLMClient, config: Config) -> None:
        self.llm = llm
        self.config = config
        self.observer: Observer = select_observer(config.observer)

    # -- planning ----------------------------------------------------------
    async def _plan(self, task: str, budget: Budget) -> Plan:
        """Run the planner, validating + repairing via corrective retries (§5.1)."""
        defect: str | None = None
        last_plan: Plan | None = None
        for _attempt in range(self.config.max_plan_retries + 1):
            sys = system_prompt("planner")
            if defect:
                sys += (
                    f"\n\nYour previous plan was invalid: {defect}. "
                    "Fix exactly that defect and return a corrected plan."
                )
            messages = [
                {"role": "system", "content": sys, "_role": "planner"},
                {"role": "user", "content": task},
            ]
            resp = await self.llm.complete(messages, tools=None,
                                           temperature=self.config.temperature_for("planner"))
            budget.note_call(resp.usage.total_tokens)
            safe(self.observer, "llm_call", role="planner", model=resp.model,
                 output_preview=resp.content or "", latency_ms=resp.latency_ms,
                 usage=resp.usage.total_tokens, tool_calls_count=0)

            obj = extract_json(resp.content or "")
            try:
                plan = Plan.model_validate(obj or {})
                last_plan = plan
                validate_plan(plan)  # raises PlanValidationError on defect
                return plan
            except PlanValidationError as e:
                defect = str(e)
            except Exception as e:  # schema mismatch from the planner
                defect = f"plan did not match schema: {e}"
        raise PlanValidationError(
            f"planner failed after {self.config.max_plan_retries + 1} attempts: {defect}"
        )

    # -- one critic-gated worker ------------------------------------------
    async def _run_gated_worker(
        self, subtask: Subtask, dep_results: dict[str, WorkerResult],
        original_task: str, budget: Budget, bump,
    ) -> tuple[WorkerResult, CriticScore]:
        initial = await run_worker(
            self.llm, subtask.role, subtask, dep_results, self.config, self.observer, budget
        )

        async def revise_fn(prev: WorkerOutput, score: CriticScore) -> WorkerOutput:
            return await revise_worker(
                self.llm, subtask.role, subtask, prev.messages,
                score.issues, score.suggestions, self.config, self.observer, budget,
            )

        out, score = await run_critic_loop(
            self.llm, subtask.role, subtask.id, original_task,
            initial, revise_fn, self.config, self.observer, budget, on_revise=bump,
        )
        safe(self.observer, "subtask_finished", subtask_id=subtask.id, role=subtask.role,
             summary=out.result.summary, confidence=out.result.confidence)
        return out.result, score

    # -- the run -----------------------------------------------------------
    async def run(self, task: str) -> FinalReport:
        budget = Budget(
            self.config.max_llm_calls, self.config.max_total_tokens, self.config.run_timeout_s
        )
        results: dict[str, WorkerResult] = {}
        scores: dict[str, CriticScore] = {}
        iters = [0]

        def bump() -> None:
            iters[0] += 1

        report_holder: dict[str, FinalReport] = {}
        finished_emitted = [False]

        def emit_finished(report: FinalReport) -> None:
            if finished_emitted[0]:
                return
            finished_emitted[0] = True
            safe(self.observer, "run_finished", summary=report.summary,
                 confidence=report.confidence, iterations=report.iterations,
                 tokens_total=report.tokens_total)

        atexit.register(lambda: None)  # backstop placeholder (real flush is in finally)

        try:
            safe(self.observer, "run_started", task=task, workspace=self.config.workspace)

            plan = await self._plan(task, budget)
            plan = augment_plan(plan, task)
            layers = validate_plan(plan)
            by_id = {s.id: s for s in plan.subtasks}
            synth_subtask = next(s for s in plan.subtasks if s.role == SYNTHESIZER)

            safe(self.observer, "plan_ready", reasoning=plan.reasoning,
                 subtask_count=len(plan.subtasks), layers=layers)

            stopped_early = False
            for layer in layers:
                worker_ids = [sid for sid in layer if by_id[sid].role != SYNTHESIZER]
                if not worker_ids:
                    continue
                if budget.exhausted():
                    stopped_early = True
                    break

                coros = [
                    self._run_gated_worker(
                        by_id[sid],
                        {d: results[d] for d in by_id[sid].depends_on if d in results},
                        task, budget, bump,
                    )
                    for sid in worker_ids
                ]
                # Fault isolation (§11): a failed sibling becomes degraded; the
                # others are NOT cancelled.
                layer_out = await asyncio.gather(*coros, return_exceptions=True)
                for sid, outcome in zip(worker_ids, layer_out):
                    if isinstance(outcome, Exception):
                        results[sid] = WorkerResult.degraded(f"worker raised: {outcome}")
                        scores[sid] = CriticScore.failed_validation()
                    else:
                        result, score = outcome
                        results[sid] = result
                        scores[sid] = score

            # Synthesis + final-answer verification (§6.3).
            final_output, final_score = await run_synthesis(
                self.llm, synth_subtask, results, task, self.config,
                self.observer, budget, on_revise=bump,
            )
            results[synth_subtask.id] = final_output.result
            scores[synth_subtask.id] = final_score
            safe(self.observer, "subtask_finished", subtask_id=synth_subtask.id,
                 role=SYNTHESIZER, summary=final_output.result.summary,
                 confidence=final_output.result.confidence)

            confidence = final_output.result.confidence
            budget_reason = budget.exhausted()
            if stopped_early or budget_reason:
                confidence = min(confidence, DEGRADED_CONFIDENCE)
            summary = final_output.result.summary
            if budget_reason:
                summary += f"\n[NOTE: budget exhausted — {budget_reason}; result is partial]"

            report = FinalReport(
                summary=summary,
                confidence=confidence,
                subtask_results=results,
                critic_scores=scores,
                iterations=iters[0],
                tokens_total=budget.total_tokens,
            )
            report_holder["report"] = report
            emit_finished(report)
            return report
        finally:
            # Authoritative cleanup (§9): emit run_finished (if not already) and
            # flush while the event loop is still alive.
            if not finished_emitted[0]:
                partial = report_holder.get("report") or FinalReport(
                    summary="run did not complete", confidence=DEGRADED_CONFIDENCE,
                    subtask_results=results, critic_scores=scores,
                    iterations=iters[0], tokens_total=budget.total_tokens,
                )
                emit_finished(partial)
            safe(self.observer, "flush")
