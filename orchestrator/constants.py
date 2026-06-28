"""Shared constants and named magic values (SPEC §8, §10).

``DEGRADED_CONFIDENCE`` is the single source of truth for the confidence
assigned to any degraded result. It is referenced everywhere a degraded
result is constructed (§8) and everywhere the synthesizer routes on a
low-confidence input (§4.1, §11) — no duplicated magic numbers.
"""

from __future__ import annotations

# --- Confidence -----------------------------------------------------------
DEGRADED_CONFIDENCE: float = 0.1

# --- Role names -----------------------------------------------------------
PLANNER = "planner"
RESEARCHER = "researcher"
CODER = "coder"
CRITIC = "critic"
SYNTHESIZER = "synthesizer"

# Roles a planner may assign to subtasks (planner/critic are orchestrator-driven).
ALLOWED_PLAN_ROLES: frozenset[str] = frozenset({RESEARCHER, CODER, SYNTHESIZER})

# Roles that run the shared worker loop (§4) and produce a WorkerResult.
WORKER_ROLES: frozenset[str] = frozenset({RESEARCHER, CODER, SYNTHESIZER})

# --- Knob defaults (§10) --------------------------------------------------
DEFAULT_APPROVAL_THRESHOLD = 8.0
DEFAULT_MAX_ITERATIONS_PER_SUBTASK = 1
DEFAULT_MAX_STEPS = 8
DEFAULT_WORKER_CONTEXT_BUDGET = 120_000  # tokens (approx; see §4.2)
DEFAULT_MAX_LLM_CALLS = 200
DEFAULT_RUN_TIMEOUT_S = 600.0
DEFAULT_ROLE_TEMPERATURE: dict[str, float] = {
    PLANNER: 0.0,
    CRITIC: 0.0,
    "default": 0.2,
}

# --- Tool/sandbox limits (§7) --------------------------------------------
MAX_FILE_READ_BYTES = 200_000
MAX_DEP_CONTEXT_BYTES = 200_000
SHELL_TIMEOUT_S = 30
SHELL_OUTPUT_CAP = 20_000

# --- LLM client -----------------------------------------------------------
DEFAULT_LLM_RETRIES = 3
