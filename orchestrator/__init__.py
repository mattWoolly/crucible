"""Minimal agentic orchestrator (see SPEC.md).

Public API:
    Orchestrator, Config, LLMSettings
    OpenAIClient, FakeLLMClient, LLMClient
    FinalReport, WorkerResult, CriticScore, Plan, Subtask
    Observer, NoopObserver, JSONLObserver, LangfuseObserver
"""

from .anthropic_client import AnthropicClient
from .config import Config, LLMSettings
from .constants import DEGRADED_CONFIDENCE
from .errors import BudgetExhausted, LLMError, OrchestratorError, PlanValidationError
from .llm import FakeLLMClient, LLMClient, OpenAIClient
from .providers import (
    PROVIDERS,
    Provider,
    ResolvedProvider,
    available_providers,
    make_client,
    resolve_from_env,
    resolve_provider,
)
from .models import CriticScore, FinalReport, Plan, Subtask, VerifyResult, WorkerResult
from .operator import Operator
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
    "Operator",
    "Config",
    "LLMSettings",
    "OpenAIClient",
    "AnthropicClient",
    "FakeLLMClient",
    "LLMClient",
    "Provider",
    "PROVIDERS",
    "ResolvedProvider",
    "available_providers",
    "make_client",
    "resolve_from_env",
    "resolve_provider",
    "FinalReport",
    "WorkerResult",
    "CriticScore",
    "Plan",
    "Subtask",
    "VerifyResult",
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
