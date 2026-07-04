"""Schema-validated outputs the orchestrator routes on (SPEC §6.1, §8, §13).

Every role except ``planner`` ultimately produces a ``WorkerResult``; the
critic produces a ``CriticScore``; the run produces a ``FinalReport``. Routing
on structure + confidence (not vibes) is a core quality driver (§13.6).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .constants import DEGRADED_CONFIDENCE


class Subtask(BaseModel):
    """A single node in the plan DAG (§5.1)."""

    id: str
    role: str
    task: str
    depends_on: list[str] = Field(default_factory=list)
    # Free-text hint about what this subtask needs from upstream (§4.1). Not a
    # routing key — routing is by ``depends_on``.
    inputs: str = ""


class Plan(BaseModel):
    """The planner's output: a DAG of subtasks (§5.1)."""

    reasoning: str = ""
    subtasks: list[Subtask] = Field(default_factory=list)


class WorkerResult(BaseModel):
    """The distilled output of any worker role (§4.1, §8).

    Only ``summary`` + ``artifacts`` are injected downstream (not the full
    transcript), preserving per-worker isolation (§13.2).
    """

    summary: str
    artifacts: dict = Field(default_factory=dict)
    confidence: float = 0.5
    uncertainties: list[str] = Field(default_factory=list)

    @classmethod
    def degraded(cls, reason: str, raw: str = "") -> "WorkerResult":
        """Construct the canonical degraded result (§8). The single place a
        degraded WorkerResult is built outside of explicit confidence values."""
        return cls(
            summary=raw[:500],
            artifacts={},
            confidence=DEGRADED_CONFIDENCE,
            uncertainties=[f"parse_failed: {reason}"],
        )

    @property
    def is_degraded(self) -> bool:
        return self.confidence <= DEGRADED_CONFIDENCE


class RubricItem(BaseModel):
    """One line-item of a per-role rubric evaluation (§6.1)."""

    criterion: str
    passed: bool
    note: str = ""


class CriticScore(BaseModel):
    """A critic's structured judgement of a result (§6, §6.1)."""

    score: float
    approved: bool
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    rubric: list[RubricItem] = Field(default_factory=list)

    @classmethod
    def failed_validation(cls) -> "CriticScore":
        """Default when the critic's own output cannot be parsed (§11):
        a middling, unapproved score so the worker iterates."""
        return cls(
            score=5.0,
            approved=False,
            issues=["critic output failed validation"],
        )


class VerifyResult(BaseModel):
    """Outcome of the optional project verify gate (e.g. ruff + pytest).

    ``passed`` is ground truth from actually running the command; ``output`` is
    the (truncated) combined stdout/stderr fed back to a repair worker.
    """

    passed: bool
    output: str = ""


class FinalReport(BaseModel):
    """What ``run()`` returns (§2 FinalReport)."""

    summary: str
    confidence: float
    subtask_results: dict[str, WorkerResult] = Field(default_factory=dict)
    critic_scores: dict[str, CriticScore] = Field(default_factory=dict)
    iterations: int = 0
    tokens_total: int = 0
    # None = no verify gate ran; True/False = the gate's final ground-truth verdict.
    verified: bool | None = None
