from orchestrator.budget import Budget


def test_call_ceiling_triggers():
    b = Budget(max_llm_calls=2, max_total_tokens=None, run_timeout_s=1000)
    b.note_call(5)
    assert b.exhausted() is None
    b.note_call(5)
    assert b.exhausted() and "max_llm_calls" in b.exhausted()


def test_token_ceiling_triggers():
    b = Budget(max_llm_calls=100, max_total_tokens=10, run_timeout_s=1000)
    b.note_call(4)
    assert b.exhausted() is None
    b.note_call(8)
    assert "max_total_tokens" in b.exhausted()


def test_none_token_budget_never_triggers_on_tokens():
    b = Budget(max_llm_calls=100, max_total_tokens=None, run_timeout_s=1000)
    for _ in range(10):
        b.note_call(10_000_000)
    assert b.exhausted() is None


def test_time_ceiling_triggers():
    t = {"v": 0.0}
    b = Budget(max_llm_calls=100, max_total_tokens=None, run_timeout_s=5, clock=lambda: t["v"])
    assert b.exhausted() is None
    t["v"] = 6.0
    assert "run_timeout_s" in b.exhausted()
