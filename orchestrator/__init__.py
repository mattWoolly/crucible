"""Minimal agentic orchestrator (see SPEC.md).

Public API:
    Orchestrator, Config, LLMSettings
    OpenAIClient, FakeLLMClient, LLMClient
    FinalReport, WorkerResult, CriticScore, Plan, Subtask
    Observer, NoopObserver, JSONLObserver, LangfuseObserver
"""

from .config import Config, LLMSettings
from .constants import DEGRADED_CONFIDENCE
from .errors import BudgetExhausted, LLMError, OrchestratorError, PlanValidationError
from .llm import FakeLLMClient, LLMClient, OpenAIClient
from .models import CriticScore, FinalReport, Plan, Subtask, WorkerResult
from .observers import (
    JSONLObserver,
    LangfuseObserver,
    NoopObserver,
    Observer,
    select_observer,
)
from .orchestrator import Orchestrator

__all__ = [
    "Orchestrator",
    "Config",
    "LLMSettings",
    "OpenAIClient",
    "FakeLLMClient",
    "LLMClient",
    "FinalReport",
    "WorkerResult",
    "CriticScore",
    "Plan",
    "Subtask",
    "Observer",
    "NoopObserver",
    "JSONLObserver",
    "LangfuseObserver",
    "select_observer",
    "DEGRADED_CONFIDENCE",
    "OrchestratorError",
    "PlanValidationError",
    "BudgetExhausted",
    "LLMError",
]
