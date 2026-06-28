"""Global run budget tracker (SPEC §10, §11).

Tracks LLM call count, total tokens, and wall-clock against ceilings. On
exhaustion the orchestrator stops spawning new work and synthesizes from
whatever exists (§11) — the budget never raises by itself.
"""

from __future__ import annotations

import time


class Budget:
    def __init__(
        self,
        max_llm_calls: int,
        max_total_tokens: int | None,
        run_timeout_s: float,
        *,
        clock=time.monotonic,
    ) -> None:
        self.max_llm_calls = max_llm_calls
        self.max_total_tokens = max_total_tokens
        self.run_timeout_s = run_timeout_s
        self._clock = clock
        self._start = clock()
        self.llm_calls = 0
        self.total_tokens = 0

    def note_call(self, total_tokens: int = 0) -> None:
        self.llm_calls += 1
        self.total_tokens += int(total_tokens or 0)

    def elapsed_s(self) -> float:
        return self._clock() - self._start

    def exhausted(self) -> str | None:
        """Return a human-readable reason if any ceiling is hit, else None."""
        if self.llm_calls >= self.max_llm_calls:
            return f"max_llm_calls ({self.max_llm_calls}) reached"
        if self.max_total_tokens is not None and self.total_tokens >= self.max_total_tokens:
            return f"max_total_tokens ({self.max_total_tokens}) reached"
        if self.elapsed_s() >= self.run_timeout_s:
            return f"run_timeout_s ({self.run_timeout_s}) reached"
        return None
