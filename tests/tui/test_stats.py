"""RunStats accounting (orchestrator/tui/stats.py) — injected clock, no sleeps."""

from orchestrator.tui.stats import RunStats, fmt_tokens


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_fmt_tokens():
    assert fmt_tokens(999) == "999"
    assert fmt_tokens(12_345) == "12.3k"
    assert fmt_tokens(3_400_000) == "3.4M"


def test_llm_call_accumulates_tokens_and_roles():
    s = RunStats(clock=FakeClock())
    s.note_event("llm_call", {"usage": 1000, "role": "coder"})
    s.note_event("llm_call", {"usage": 500, "role": "coder"})
    s.note_event("llm_call", {"usage": 200, "role": "critic"})
    assert s.total_tokens == 1700
    assert s.llm_calls == 3
    assert s.tokens_by_role == {"coder": 1500, "critic": 200}


def test_missing_usage_counts_as_zero_not_crash():
    s = RunStats(clock=FakeClock())
    s.note_event("llm_call", {"usage": None, "role": "planner"})
    assert s.total_tokens == 0 and s.llm_calls == 1


def test_tool_calls_counted():
    s = RunStats(clock=FakeClock())
    s.note_event("tool_call", {"name": "run_shell"})
    assert s.tool_calls == 1


def test_elapsed_tracks_active_run_then_freezes():
    clock = FakeClock()
    s = RunStats(clock=clock)
    s.note_event("run_started", {})
    clock.now += 30
    assert s.elapsed_seconds() == 30
    s.note_event("run_finished", {})
    clock.now += 999
    assert s.elapsed_seconds() == 30  # frozen at run end


def test_status_line_with_and_without_token_ceiling():
    s = RunStats(clock=FakeClock())
    s.note_event("llm_call", {"usage": 50_000, "role": "coder"})
    assert "tokens 50.0k" in s.status_line(200, None)
    line = s.status_line(200, 100_000)
    assert "50.0k/100.0k (50%)" in line and "llm 1/200" in line


def test_detail_lines_sorted_by_role_spend():
    s = RunStats(clock=FakeClock())
    s.note_event("llm_call", {"usage": 10, "role": "critic"})
    s.note_event("llm_call", {"usage": 90, "role": "coder"})
    lines = s.detail_lines(200, None)
    joined = "\n".join(lines)
    assert joined.index("coder") < joined.index("critic")


def test_reset_session_zeroes_everything():
    s = RunStats(clock=FakeClock())
    s.note_event("llm_call", {"usage": 10, "role": "coder"})
    s.reset_session()
    assert s.total_tokens == 0 and s.llm_calls == 0 and s.tokens_by_role == {}
