"""Exception types for the orchestrator (SPEC §11)."""

from __future__ import annotations


class OrchestratorError(Exception):
    """Base class for orchestrator errors."""


class PlanValidationError(OrchestratorError):
    """Raised when a plan is structurally invalid and cannot be repaired (§5.1).

    The message names the specific defect so it can be fed back to the
    planner as a corrective prompt.
    """


class BudgetExhausted(OrchestratorError):
    """Raised when a global budget ceiling is hit and no work can proceed (§10)."""


class LLMError(OrchestratorError):
    """Raised when the LLM client exhausts its retries (§11)."""
