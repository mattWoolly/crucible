"""OTel telemetry (orchestrator/otel.py, WI-34) — hermetic: in-memory
metric reader + span exporter injected; no network, no collector."""

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from orchestrator.observers import MultiObserver, Observer
from orchestrator.otel import (
    OTelObserver,
    TelemetryInternals,
    TelemetrySettings,
    create_otel_observer,
)


@pytest.fixture()
def harness():
    reader = InMemoryMetricReader()
    spans = InMemorySpanExporter()
    obs = OTelObserver(
        TelemetrySettings(enabled=True, environment="test"),
        provider_label="glm",
        internals=TelemetryInternals(
            metric_reader=reader, span_processor=SimpleSpanProcessor(spans)
        ),
    )
    return obs, reader, spans


def metric_points(reader, name):
    data = reader.get_metrics_data()
    out = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    out.extend(metric.data.data_points)
    return out


def finished(spans):
    return {s.name: s for s in spans.get_finished_spans()}


# -- enablement -----------------------------------------------------------------


def test_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("CRUCIBLE_OTEL", raising=False)
    assert create_otel_observer() is None
    assert create_otel_observer(TelemetrySettings(enabled=False)) is None


def test_env_settings(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_OTEL", "1")
    monkeypatch.setenv("CRUCIBLE_OTEL_ENDPOINT", "http://otherhost:4317")
    monkeypatch.setenv("CRUCIBLE_OTEL_ENV", "prod")
    monkeypatch.setenv("CRUCIBLE_OTEL_TRACES", "0")
    s = TelemetrySettings.load()
    assert s.enabled and s.endpoint == "http://otherhost:4317"
    assert s.environment == "prod" and s.traces is False


def write_config(tmp_path, monkeypatch, body: str) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(body)
    monkeypatch.setenv("CRUCIBLE_CONFIG", str(cfg))


def test_config_file_enables_persistently(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch,
                 '{"otel": {"enabled": true, "endpoint": "http://filehost:4317"}}')
    s = TelemetrySettings.load()
    assert s.enabled is True
    assert s.endpoint == "http://filehost:4317"
    assert s.traces is True  # file omits it -> default


def test_env_overrides_config_file_both_ways(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, '{"otel": {"enabled": true}}')
    monkeypatch.setenv("CRUCIBLE_OTEL", "0")  # env force-disables
    assert TelemetrySettings.load().enabled is False
    write_config(tmp_path, monkeypatch, '{"otel": {"enabled": false}}')
    monkeypatch.setenv("CRUCIBLE_OTEL", "1")  # env force-enables
    assert TelemetrySettings.load().enabled is True


def test_corrupt_or_missing_config_falls_back_to_defaults(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, "{not json")
    s = TelemetrySettings.load()
    assert s.enabled is False and s.endpoint == "http://localhost:4317"
    monkeypatch.setenv("CRUCIBLE_CONFIG", str(tmp_path / "missing.json"))
    assert TelemetrySettings.load().enabled is False


# -- metrics ---------------------------------------------------------------------


def test_llm_call_fans_tokens_by_type(harness):
    obs, reader, _ = harness
    obs.llm_call(role="coder", model="glm-5.2", usage=150,
                 usage_prompt=100, usage_completion=50, latency_ms=1200.0)
    points = metric_points(reader, "crucible.token.usage")
    by_type = {p.attributes["type"]: p.value for p in points}
    assert by_type == {"input": 100, "output": 50}
    for p in points:
        assert p.attributes["provider"] == "glm"
        assert p.attributes["model"] == "glm-5.2"
        assert p.attributes["role"] == "coder"
        assert "subtask_id" not in p.attributes  # high-card ids: spans only
    dur = metric_points(reader, "crucible.llm.duration")
    assert dur and dur[0].sum == 1200.0


def test_total_only_usage_falls_back_to_total_type(harness):
    obs, reader, _ = harness
    obs.llm_call(role="planner", model="m", usage=99, latency_ms=1.0)
    points = metric_points(reader, "crucible.token.usage")
    assert [(p.attributes["type"], p.value) for p in points] == [("total", 99)]


def test_zero_token_types_are_skipped(harness):
    obs, reader, _ = harness
    obs.llm_call(role="coder", model="m", usage=10, usage_prompt=10,
                 usage_completion=0, latency_ms=1.0)
    points = metric_points(reader, "crucible.token.usage")
    assert {p.attributes["type"] for p in points} == {"input"}


def test_tool_verify_critic_run_counters(harness):
    obs, reader, _ = harness
    obs.run_started(task="t", workspace="w")
    obs.tool_call(name="run_shell", args={}, latency_ms=30.0, error=None)
    obs.tool_call(name="write_file", args={}, latency_ms=5.0, error="ERROR: denied")
    obs.verify(attempt=0, passed=False)
    obs.verify(attempt=1, passed=True)
    obs.critic_score(subtask_id="s1", score=8.5, approved=True)
    obs.run_finished(confidence=0.9, tokens_total=100)

    runs = metric_points(reader, "crucible.run.count")
    assert runs[0].value == 1
    tools = {(p.attributes["tool"], p.attributes["ok"]): p.value
             for p in metric_points(reader, "crucible.tool.count")}
    assert tools == {("run_shell", "true"): 1, ("write_file", "false"): 1}
    verifies = {p.attributes["passed"]: p.value
                for p in metric_points(reader, "crucible.verify.count")}
    assert verifies == {"true": 1, "false": 1}
    critic = metric_points(reader, "crucible.critic.score")
    assert critic and critic[0].sum == 8.5


def test_subtask_duration_recorded_by_role(harness):
    obs, reader, _ = harness
    obs.run_started()
    obs.subtask_started(subtask_id="s1", role="coder", task="t")
    obs.subtask_finished(subtask_id="s1", role="coder", summary="ok", confidence=0.9)
    points = metric_points(reader, "crucible.subtask.duration")
    assert points and points[0].attributes["role"] == "coder"


# -- traces -----------------------------------------------------------------------


def test_span_hierarchy_run_subtask_tool(harness):
    obs, _, spans = harness
    obs.run_started(task="t")
    obs.subtask_started(subtask_id="s1", role="coder", task="t")
    obs.llm_call(subtask_id="s1", role="coder", model="m", usage=10,
                 usage_prompt=5, usage_completion=5, latency_ms=100.0)
    obs.tool_call(subtask_id="s1", name="run_shell", args={}, latency_ms=20.0, error=None)
    obs.subtask_finished(subtask_id="s1", role="coder", summary="ok", confidence=0.9)
    obs.run_finished(confidence=0.9, tokens_total=10)

    by_name = finished(spans)
    assert set(by_name) == {"crucible.run", "crucible.subtask", "crucible.llm", "crucible.tool"}
    run = by_name["crucible.run"]
    sub = by_name["crucible.subtask"]
    assert sub.parent is not None and sub.parent.span_id == run.context.span_id
    assert by_name["crucible.llm"].parent.span_id == sub.context.span_id
    assert by_name["crucible.tool"].parent.span_id == sub.context.span_id
    assert sub.attributes["subtask.id"] == "s1"  # high-card id lives on the span
    assert by_name["crucible.tool"].attributes["ok"] is True
    # retroactive timing: llm span duration ≈ latency
    llm = by_name["crucible.llm"]
    assert 90 <= (llm.end_time - llm.start_time) / 1e6 <= 110


def test_planner_llm_call_parents_to_run_span(harness):
    obs, _, spans = harness
    obs.run_started()
    obs.llm_call(role="planner", model="m", usage=10, latency_ms=10.0)  # no subtask_id
    obs.run_finished(confidence=1.0, tokens_total=10)
    by_name = finished(spans)
    assert by_name["crucible.llm"].parent.span_id == by_name["crucible.run"].context.span_id


def test_crashed_run_still_closes_spans(harness):
    obs, _, spans = harness
    obs.run_started()
    obs.subtask_started(subtask_id="s1", role="coder", task="t")
    obs.run_finished(summary="run did not complete", confidence=0.1)  # no subtask_finished
    names = [s.name for s in spans.get_finished_spans()]
    assert names.count("crucible.subtask") == 1 and "crucible.run" in names


def test_traces_disabled_still_records_metrics():
    reader = InMemoryMetricReader()
    obs = OTelObserver(
        TelemetrySettings(enabled=True, traces=False, environment="t"),
        internals=TelemetryInternals(metric_reader=reader),
    )
    obs.run_started()
    obs.llm_call(role="coder", model="m", usage=5, usage_prompt=5, latency_ms=1.0)
    obs.run_finished(confidence=1.0, tokens_total=5)
    assert metric_points(reader, "crucible.token.usage")


def test_resource_attributes_set(harness):
    obs, reader, _ = harness
    obs.run_started()
    data = reader.get_metrics_data()
    attrs = dict(data.resource_metrics[0].resource.attributes)
    assert attrs["service.name"] == "crucible"
    assert attrs["env"] == "test"


def test_flush_and_close_never_raise(harness):
    obs, _, _ = harness
    obs.flush()
    obs.close()
    obs.close()  # idempotent


# -- MultiObserver ------------------------------------------------------------------


def test_multi_observer_fans_out_and_isolates_failures():
    seen: list[tuple[str, dict]] = []

    class Spy(Observer):
        def llm_call(self, **f):
            seen.append(("llm_call", f))

        def flush(self):
            seen.append(("flush", {}))

    class Broken(Observer):
        def llm_call(self, **f):
            raise RuntimeError("boom")

        def close(self):
            raise RuntimeError("boom")

    multi = MultiObserver([Broken(), Spy()])
    multi.llm_call(role="coder", usage=1)  # Broken must not starve Spy
    multi.flush()
    multi.close()  # Broken.close swallowed
    assert [e for e, _ in seen] == ["llm_call", "flush"]
    assert seen[0][1]["role"] == "coder"
