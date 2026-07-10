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

# Per-request timeout (seconds). Without this a stalled provider response wedges
# the whole run indefinitely: the overall run_timeout_s is only checked BETWEEN
# operations, so a single hung network read is never interrupted. A real call
# (even a large reasoning-model plan) returns well under this; a hang trips it,
# raising a retryable APITimeoutError that the client's own backoff path handles.
DEFAULT_REQUEST_TIMEOUT_S = 300.0

# Rate-limit handling (429 from a burst of parallel workers): dedicated retry
# budget with long, capped, jittered backoff; and a cap on concurrent requests.
DEFAULT_RATE_LIMIT_RETRIES = 6
DEFAULT_RATE_LIMIT_BACKOFF_S = 10.0
RATE_LIMIT_BACKOFF_CAP_S = 60.0
DEFAULT_MAX_CONCURRENCY = 4

# Project verify gate: default number of repair passes after a failed verify.
DEFAULT_MAX_VERIFY_REPAIRS = 3
