"""Configuration knobs (SPEC §10).

Knobs are constructor args with defaults; environment variables feed the LLM
client and observer selection. ``Config`` itself is a plain dataclass so it is
trivial to construct in tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from . import constants as C


@dataclass
class Config:
    workspace: str  # required (§10): the sandboxed directory agents operate in.

    approval_threshold: float = C.DEFAULT_APPROVAL_THRESHOLD
    approval_threshold_by_role: dict[str, float] = field(default_factory=dict)
    max_iterations_per_subtask: int = C.DEFAULT_MAX_ITERATIONS_PER_SUBTASK
    max_steps: int = C.DEFAULT_MAX_STEPS
    worker_context_budget: int = C.DEFAULT_WORKER_CONTEXT_BUDGET
    max_llm_calls: int = C.DEFAULT_MAX_LLM_CALLS
    max_total_tokens: int | None = None
    run_timeout_s: float = C.DEFAULT_RUN_TIMEOUT_S
    role_temperature: dict[str, float] = field(
        default_factory=lambda: dict(C.DEFAULT_ROLE_TEMPERATURE)
    )
    observer: Any | None = None  # avoid import cycle; observers.Observer at runtime

    # Plan validation retry budget (§5.1).
    max_plan_retries: int = 3

    # Optional project verify gate: how many repair passes to attempt when the
    # injected verifier reports failure after synthesis (0 = verify but never
    # repair). The verifier itself is injected into Orchestrator, not here.
    max_verify_repairs: int = C.DEFAULT_MAX_VERIFY_REPAIRS

    def threshold_for(self, role: str) -> float:
        """Per-role threshold override falls back to the base threshold (§6.4, §10)."""
        return self.approval_threshold_by_role.get(role, self.approval_threshold)

    def temperature_for(self, role: str) -> float:
        """Role temperature with a 'default' fallback (§10)."""
        if role in self.role_temperature:
            return self.role_temperature[role]
        return self.role_temperature.get("default", 0.2)

    @classmethod
    def from_env(cls, workspace: str, **overrides: Any) -> "Config":
        """Build a Config from env + explicit overrides. Only knobs the env
        meaningfully controls are read here; the rest use defaults."""
        return cls(workspace=workspace, **overrides)


@dataclass
class LLMSettings:
    """Env-sourced LLM connection settings (§10)."""

    api_key: str | None = None
    base_url: str | None = None
    model: str = "default"

    @classmethod
    def from_env(cls) -> "LLMSettings":
        return cls(
            api_key=os.environ.get("LLM_API_KEY"),
            base_url=os.environ.get("LLM_BASE_URL"),
            model=os.environ.get("LLM_MODEL", "default"),
        )
