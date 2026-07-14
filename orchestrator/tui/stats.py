"""Session token / call accounting for the TUI footer and /tokens (pure).

Fed from Observer events (``llm_call`` carries per-call token usage), so the
numbers shown are exactly what the engine's Budget saw — no second counter
inside the engine.
"""

from __future__ import annotations

import time


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


class RunStats:
    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self.reset_session()

    def reset_session(self) -> None:
        self.total_tokens = 0
        self.llm_calls = 0
        self.tool_calls = 0
        self.tokens_by_role: dict[str, int] = {}
        self.runs = 0
        self.run_active = False
        self._run_started_at: float | None = None
        self.last_run_seconds = 0.0

    # -- event feed ---------------------------------------------------------
    def note_event(self, event: str, fields: dict) -> None:
        if event == "run_started":
            self.runs += 1
            self.run_active = True
            self._run_started_at = self._clock()
        elif event == "run_finished":
            self.run_active = False
            if self._run_started_at is not None:
                self.last_run_seconds = self._clock() - self._run_started_at
        elif event == "llm_call":
            self.llm_calls += 1
            usage = int(fields.get("usage") or 0)
            self.total_tokens += usage
            role = str(fields.get("role") or "?")
            self.tokens_by_role[role] = self.tokens_by_role.get(role, 0) + usage
        elif event == "tool_call":
            self.tool_calls += 1

    # -- rendering ----------------------------------------------------------
    def elapsed_seconds(self) -> float:
        if self.run_active and self._run_started_at is not None:
            return self._clock() - self._run_started_at
        return self.last_run_seconds

    def status_line(self, max_llm_calls: int, max_total_tokens: int | None) -> str:
        toks = fmt_tokens(self.total_tokens)
        if max_total_tokens:
            pct = 100.0 * self.total_tokens / max_total_tokens
            tok_part = f"{toks}/{fmt_tokens(max_total_tokens)} ({pct:.0f}%)"
        else:
            tok_part = toks
        parts = [
            f"tokens {tok_part}",
            f"llm {self.llm_calls}/{max_llm_calls}",
            f"tools {self.tool_calls}",
        ]
        secs = self.elapsed_seconds()
        if secs or self.run_active:
            parts.append(f"{secs:.0f}s")
        return " · ".join(parts)

    def detail_lines(self, max_llm_calls: int, max_total_tokens: int | None) -> list[str]:
        lines = [
            f"session tokens : {self.total_tokens:,}"
            + (f" of {max_total_tokens:,} budget" if max_total_tokens else " (no token ceiling)"),
            f"llm calls      : {self.llm_calls} (ceiling {max_llm_calls} per run)",
            f"tool calls     : {self.tool_calls}",
            f"runs           : {self.runs}",
        ]
        if self.tokens_by_role:
            lines.append("by role:")
            for role, toks in sorted(self.tokens_by_role.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {role:<12} {fmt_tokens(toks):>8}")
        return lines
