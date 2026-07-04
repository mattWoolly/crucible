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
from typing import Awaitable, Callable

from .augmentation import augment_plan
from .budget import Budget
from .config import Config
from .constants import CODER, DEGRADED_CONFIDENCE, SYNTHESIZER
from .critic import run_critic_loop
from .errors import LLMError, PlanValidationError
from .json_extract import extract_json
from .llm import LLMClient
from .models import CriticScore, FinalReport, Plan, Subtask, VerifyResult, WorkerResult
from .observers import Observer, safe, select_observer
from .plan_validation import validate_plan
from .redaction import redact_preview
from .roles import system_prompt
from .synthesizer import run_synthesis
from .worker import WorkerOutput, revise_worker, run_worker, workspace_orientation

# Injected ground-truth gate: given the workspace path, run the project's own
# checks (e.g. ruff + pytest) and report whether they pass. Async so a real
# implementation can shell out without blocking the loop.
Verifier = Callable[[str], Awaitable[VerifyResult]]


class Orchestrator:
    def __init__(
        self,
        llm: LLMClient,
        config: Config,
        verifier: Verifier | None = None,
    ) -> None:
        self.llm = llm
        self.config = config
        # Optional injected ground-truth gate (e.g. run ruff + pytest). Kept out
        # of the engine's sandbox purity: the caller supplies the runner.
        self.verifier = verifier
        self.observer: Observer = select_observer(config.observer)

    async def _verify_and_repair(
        self, task: str, results: dict, scores: dict, budget: Budget, bump,
    ) -> VerifyResult:
        """Run the injected verifier; on failure, drive a bounded repair loop
        that feeds the real command output back to a coder, then re-verify.

        The final workspace state is either verified green or the run reports
        ``verified=False`` honestly — no more rubber-stamped 'done'."""
        vres = await self.verifier(self.config.workspace)
        safe(self.observer, "verify", attempt=0, passed=vres.passed,
             output_preview=redact_preview(vres.output))
        attempt = 0
        while not vres.passed and attempt < self.config.max_verify_repairs:
            if budget.exhausted():
                break
            attempt += 1
            repair = Subtask(
                id=f"verify_repair_{attempt}",
                role=CODER,
                task=(
                    "The project verify command FAILED. Fix the code so it "
                    "passes. Do NOT delete or weaken tests merely to make them "
                    "pass — fix the underlying cause. Verify output:\n"
                    f"{vres.output}"
                ),
            )
            safe(self.observer, "subtask_started", subtask_id=repair.id,
                 role=CODER, task=repair.task, dependency_ids=[])
            out = await run_worker(
                self.llm, CODER, repair, {}, self.config, self.observer, budget
            )
            results[repair.id] = out.result
            scores[repair.id] = CriticScore.failed_validation()
            bump()
            safe(self.observer, "subtask_finished", subtask_id=repair.id,
                 role=CODER, summary=out.result.summary, confidence=out.result.confidence)
            vres = await self.verifier(self.config.workspace)
            safe(self.observer, "verify", attempt=attempt, passed=vres.passed,
                 output_preview=redact_preview(vres.output))
        return vres

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
            # Ground the planner with the actual workspace contents so agentic
            # models don't try to "explore first" (they have no tools here).
            user = workspace_orientation(self.config.workspace) + "\n\n" + task
            messages = [
                {"role": "system", "content": sys, "_role": "planner"},
                {"role": "user", "content": user},
            ]
            resp = await self.llm.complete(messages, tools=None,
                                           temperature=self.config.temperature_for("planner"))
            budget.note_call(resp.usage.total_tokens)
            safe(self.observer, "llm_call", role="planner", model=resp.model,
                 output_preview=resp.content or "", latency_ms=resp.latency_ms,
                 usage=resp.usage.total_tokens, tool_calls_count=0)

            obj = extract_json(resp.content or "")
            if obj is None:
                # Most often: the model emitted (pseudo) tool calls or prose
                # instead of the plan. Say so — "plan has no subtasks" won't
                # stop it from trying tools again.
                defect = (
                    "your response contained no JSON object. You have NO tools; "
                    "do not attempt tool calls — respond with the JSON plan directly"
                )
                continue
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

            # Synthesis + final-answer verification (§6.3). A persistent LLM
            # failure here (provider quota/outage) must degrade, not crash —
            # the accumulated worker results are the valuable part (§11).
            try:
                final_output, final_score = await run_synthesis(
                    self.llm, synth_subtask, results, task, self.config,
                    self.observer, budget, on_revise=bump,
                )
                synth_result = final_output.result
            except LLMError as exc:
                # raw doubles as the summary: there is no model output here,
                # and the report must say why synthesis is missing.
                synth_result = WorkerResult.degraded(
                    f"synthesis failed: {exc}", raw=f"synthesis failed: {exc}"
                )
                final_score = CriticScore.failed_validation()
            results[synth_subtask.id] = synth_result
            scores[synth_subtask.id] = final_score
            safe(self.observer, "subtask_finished", subtask_id=synth_subtask.id,
                 role=SYNTHESIZER, summary=synth_result.summary,
                 confidence=synth_result.confidence)

            # Ground-truth verify gate (if injected): the run's definition of
            # done, enforced by actually running the command — not the critic's
            # say-so. Repairs happen inside; verified reflects the final state.
            verified: bool | None = None
            if self.verifier is not None:
                vres = await self._verify_and_repair(task, results, scores, budget, bump)
                verified = vres.passed

            confidence = synth_result.confidence
            budget_reason = budget.exhausted()
            if stopped_early or budget_reason:
                confidence = min(confidence, DEGRADED_CONFIDENCE)
            summary = synth_result.summary
            if budget_reason:
                summary += f"\n[NOTE: budget exhausted — {budget_reason}; result is partial]"
            if verified is False:
                confidence = min(confidence, DEGRADED_CONFIDENCE)
                summary += (
                    "\n[NOTE: verify gate FAILED after repair attempts — the "
                    "workspace does not pass its own checks]"
                )
            elif verified is True:
                summary += "\n[verify gate: PASSED]"

            report = FinalReport(
                summary=summary,
                confidence=confidence,
                subtask_results=results,
                critic_scores=scores,
                iterations=iters[0],
                tokens_total=budget.total_tokens,
                verified=verified,
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
