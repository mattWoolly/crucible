import io
import json

from orchestrator.observers import (
    EVENTS,
    JSONLObserver,
    NoopObserver,
    Observer,
    select_observer,
    safe,
)


class _Boom(Observer):
    def llm_call(self, **f):
        raise RuntimeError("observer is buggy")


def test_safe_swallows_observer_exception():
    safe(_Boom(), "llm_call", role="coder")  # must not raise


def test_safe_handles_none_observer():
    safe(None, "run_started", task="x")  # no-op


def test_jsonl_emits_one_line_per_event():
    buf = io.StringIO()
    obs = JSONLObserver(stream=buf)
    obs.run_started(task="t", workspace="/ws")
    obs.subtask_finished(subtask_id="a", role="coder", summary="s", confidence=0.9)
    lines = [l for l in buf.getvalue().splitlines() if l.strip()]
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["event"] == "run_started" and rec["task"] == "t"


def test_base_observer_implements_every_declared_event():
    # safe() dispatches by attribute name; a declared event with no method
    # would be silently dropped (the verify-gate regression).
    obs = Observer()
    for event in EVENTS:
        assert callable(getattr(obs, event)), f"Observer missing {event}"


def test_jsonl_persists_verify_event():
    buf = io.StringIO()
    obs = JSONLObserver(stream=buf)
    obs.verify(attempt=1, passed=False, output_preview="pytest: 2 failed")
    rec = json.loads(buf.getvalue().splitlines()[0])
    assert rec["event"] == "verify"
    assert rec["passed"] is False and rec["attempt"] == 1


def test_jsonl_verify_output_keeps_the_tail():
    # The pytest summary lives at the END of the output. A verify preview must
    # keep the tail so "did failures shrink each pass?" is answerable from the
    # trace — unlike a normal preview, which head-truncates.
    buf = io.StringIO()
    obs = JSONLObserver(stream=buf)
    long_output = ("x" * 6000) + "\n=== 3 failed, 40 passed in 9.1s ==="
    obs.verify(attempt=2, passed=False, output_preview=long_output)
    rec = json.loads(buf.getvalue().splitlines()[0])
    assert "3 failed, 40 passed" in rec["output_preview"]  # summary survived
    assert len(rec["output_preview"]) < len(long_output)   # still bounded


def test_non_verify_preview_still_head_truncates():
    # A regular event's output_preview keeps the default head behaviour.
    buf = io.StringIO()
    obs = JSONLObserver(stream=buf)
    head_then_tail = "START-MARKER " + ("x" * 4000) + " END-MARKER"
    obs.llm_call(role="coder", output_preview=head_then_tail)
    rec = json.loads(buf.getvalue().splitlines()[0])
    assert "START-MARKER" in rec["output_preview"]
    assert "END-MARKER" not in rec["output_preview"]


def test_jsonl_redacts_secret_in_preview():
    buf = io.StringIO()
    obs = JSONLObserver(stream=buf)
    obs.llm_call(role="researcher", output_preview="found key sk-abcdef1234567890XYZ here")
    rec = json.loads(buf.getvalue().splitlines()[0])
    assert "sk-abcdef" not in rec["output_preview"]
    assert "REDACTED" in rec["output_preview"]


def test_auto_select_defaults_to_jsonl(monkeypatch):
    monkeypatch.delenv("OBSERVER_TRACING_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("OBSERVER_TRACING_SECRET_KEY", raising=False)
    monkeypatch.delenv("OBSERVER_LOG_FILE", raising=False)
    obs = select_observer()
    assert isinstance(obs, JSONLObserver)


def test_auto_select_respects_explicit():
    noop = NoopObserver()
    assert select_observer(noop) is noop
