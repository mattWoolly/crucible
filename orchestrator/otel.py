"""Native OpenTelemetry telemetry (WI-34, ported from Determa's
`@determa/telemetry` to Crucible's Observer seam).

Metrics and traces go to the local OTLP collector (gRPC :4317 — same path as
Claude Code) and land in the self-hosted Prometheus + Loki + Grafana stack.
Everything is dimensioned by ``provider``/``model``/``role`` so usage is
separable per model. NO prompt/response content leaves the process — only
counts, durations, model ids, tool names, roles, and pass/fail outcomes.

Design (mirrors the Determa implementation's load-bearing choices):
- Providers are LOCAL to the observer, never registered as OTel globals — an
  embedder's own OTel setup stays untouched.
- Span parenting is EXPLICIT (keyed by ``subtask_id``), not context-based:
  parallel workers interleave events, so ambient context would mis-parent.
- Instrument names use the ``.`` form (``crucible.token.usage``); the
  collector's OTLP→Prometheus translation converts ``.``→``_`` and appends
  ``_total`` to counters / ``_milliseconds_*`` to ms histograms.
- High-cardinality ids (subtask ids) are SPAN attributes only, never metric
  attributes.
- Test seam: in-memory reader/exporter injectable via ``TelemetryInternals``
  — unit tests never touch the network.
- Disabled (the default) → ``create_otel_observer`` returns ``None``; zero
  OTel code runs.

Wiring: chain it after the UI/JSONL observer with ``MultiObserver`` — the
engine's ``flush``/``close`` lifecycle drives force-flush and shutdown.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .observers import Observer

DEFAULT_ENDPOINT = "http://localhost:4317"
DEFAULT_CONFIG_PATH = "~/.config/crucible/config.json"
DEFAULT_EXPORT_INTERVAL_MS = 10_000
SERVICE_NAME = "crucible"
SERVICE_VERSION = "0.1.0"  # keep in sync with pyproject [project].version

_NS = 1_000_000  # ms -> ns


@dataclass(frozen=True)
class TelemetrySettings:
    enabled: bool = False
    endpoint: str = DEFAULT_ENDPOINT
    environment: str = ""
    traces: bool = True
    metric_export_interval_ms: int = DEFAULT_EXPORT_INTERVAL_MS

    @classmethod
    def load(cls) -> "TelemetrySettings":
        """Resolve settings with precedence env > config file > defaults.

        Config file: ``~/.config/crucible/config.json`` (override the path
        with ``CRUCIBLE_CONFIG``), shape ``{"otel": {"enabled": true,
        "endpoint": ..., "environment": ..., "traces": true}}``. A missing or
        corrupt file silently falls back to defaults — telemetry config must
        never break startup. Env: ``CRUCIBLE_OTEL=1`` enables (``0`` force-
        disables even when the file enables), plus ``CRUCIBLE_OTEL_ENDPOINT``,
        ``CRUCIBLE_OTEL_ENV``, ``CRUCIBLE_OTEL_TRACES=0``.
        """
        path = Path(os.environ.get("CRUCIBLE_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()
        file_cfg: dict = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("otel"), dict):
                file_cfg = raw["otel"]
        except (OSError, ValueError):
            pass

        def env_bool(name: str, fallback: bool) -> bool:
            val = os.environ.get(name)
            if val is None:
                return fallback
            return val in ("1", "true", "yes")

        return cls(
            enabled=env_bool("CRUCIBLE_OTEL", bool(file_cfg.get("enabled", False))),
            endpoint=os.environ.get("CRUCIBLE_OTEL_ENDPOINT")
            or str(file_cfg.get("endpoint") or DEFAULT_ENDPOINT),
            environment=os.environ.get("CRUCIBLE_OTEL_ENV")
            or str(file_cfg.get("environment") or "")
            or os.uname().nodename,
            traces=env_bool("CRUCIBLE_OTEL_TRACES", bool(file_cfg.get("traces", True))),
        )

    from_env = load  # backward-compatible alias (env still wins; file added)


@dataclass
class TelemetryInternals:
    """Test seam: inject in-memory reader/exporter instead of OTLP/gRPC."""

    metric_reader: Any | None = None
    span_processor: Any | None = None


class OTelObserver(Observer):
    """Observer → OTel bridge: engine events become metrics + spans."""

    def __init__(
        self,
        settings: TelemetrySettings,
        provider_label: str = "unknown",
        internals: TelemetryInternals | None = None,
    ) -> None:
        # Imports are local so the module (and package __init__) stays cheap
        # when telemetry is disabled.
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.trace import NoOpTracerProvider

        self._provider_label = provider_label
        resource = Resource.create(
            {
                "service.name": SERVICE_NAME,
                "service.version": SERVICE_VERSION,
                "env": settings.environment,
            }
        )

        if internals is not None and internals.metric_reader is not None:
            readers = [internals.metric_reader]
        else:  # pragma: no cover - real exporter path (validated live)
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

            readers = [
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=settings.endpoint, insecure=True),
                    export_interval_millis=settings.metric_export_interval_ms,
                )
            ]
        self._meter_provider = MeterProvider(metric_readers=readers, resource=resource)
        meter = self._meter_provider.get_meter(SERVICE_NAME)

        self._tokens = meter.create_counter(
            "crucible.token.usage", unit="{token}",
            description="Tokens consumed, by provider/model/role/type",
        )
        self._llm_duration = meter.create_histogram(
            "crucible.llm.duration", unit="ms",
            description="Model call latency, by provider/model/role",
        )
        self._runs = meter.create_counter(
            "crucible.run.count", description="Orchestrator runs started",
        )
        self._tools = meter.create_counter(
            "crucible.tool.count", description="Tool calls, by tool/ok",
        )
        self._tool_duration = meter.create_histogram(
            "crucible.tool.duration", unit="ms", description="Tool call latency, by tool",
        )
        self._verifies = meter.create_counter(
            "crucible.verify.count", description="Verify gate outcomes, by passed",
        )
        self._critic_scores = meter.create_histogram(
            "crucible.critic.score", description="Critic scores, by approved",
        )
        self._subtask_duration = meter.create_histogram(
            "crucible.subtask.duration", unit="ms", description="Subtask wall time, by role",
        )

        if settings.traces:
            self._tracer_provider = TracerProvider(resource=resource)
            if internals is not None and internals.span_processor is not None:
                self._tracer_provider.add_span_processor(internals.span_processor)
            else:  # pragma: no cover - real exporter path (validated live)
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )
                from opentelemetry.sdk.trace.export import BatchSpanProcessor

                self._tracer_provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.endpoint, insecure=True))
                )
        else:
            self._tracer_provider = NoOpTracerProvider()
        self._tracer = self._tracer_provider.get_tracer(SERVICE_NAME)

        # Explicit span bookkeeping (parallel workers interleave events).
        self._run_span: Any | None = None
        self._subtask_spans: dict[str, Any] = {}
        self._subtask_roles: dict[str, str] = {}
        self._subtask_started_ns: dict[str, int] = {}

    # -- span helpers -----------------------------------------------------------
    def _context_for(self, subtask_id: str | None):
        from opentelemetry import trace as otrace

        parent = self._subtask_spans.get(subtask_id or "") or self._run_span
        if parent is None:
            return None
        return otrace.set_span_in_context(parent)

    def _retro_span(self, name: str, subtask_id: str | None,
                    latency_ms: float, attrs: dict) -> None:
        """A span for an already-completed operation (events arrive after the
        fact, with latency): start/end times reconstructed."""
        end_ns = time.time_ns()
        start_ns = end_ns - int(max(latency_ms, 0.0) * _NS)
        span = self._tracer.start_span(
            name, context=self._context_for(subtask_id),
            start_time=start_ns, attributes=attrs,
        )
        span.end(end_time=end_ns)

    # -- engine events ------------------------------------------------------------
    def run_started(self, **f: Any) -> None:
        self._runs.add(1)
        # New run: drop any leftover spans from a crashed predecessor.
        self._subtask_spans.clear()
        self._subtask_roles.clear()
        self._subtask_started_ns.clear()
        self._run_span = self._tracer.start_span(
            "crucible.run", attributes={"provider": self._provider_label}
        )

    def run_finished(self, **f: Any) -> None:
        for span in self._subtask_spans.values():  # crashed run: close children
            span.end()
        self._subtask_spans.clear()
        if self._run_span is not None:
            self._run_span.set_attribute("confidence", float(f.get("confidence") or 0.0))
            self._run_span.set_attribute("tokens.total", int(f.get("tokens_total") or 0))
            self._run_span.end()
            self._run_span = None

    def subtask_started(self, **f: Any) -> None:
        sid = str(f.get("subtask_id", ""))
        role = str(f.get("role", "unknown"))
        self._subtask_roles[sid] = role
        self._subtask_started_ns[sid] = time.time_ns()
        self._subtask_spans[sid] = self._tracer.start_span(
            "crucible.subtask",
            context=self._context_for(None),
            attributes={"subtask.id": sid, "role": role},
        )

    def subtask_finished(self, **f: Any) -> None:
        sid = str(f.get("subtask_id", ""))
        span = self._subtask_spans.pop(sid, None)
        if span is not None:
            span.set_attribute("confidence", float(f.get("confidence") or 0.0))
            span.end()
        started = self._subtask_started_ns.pop(sid, None)
        if started is not None:
            self._subtask_duration.record(
                (time.time_ns() - started) / _NS,
                {"role": self._subtask_roles.pop(sid, "unknown")},
            )

    def llm_call(self, **f: Any) -> None:
        role = str(f.get("role", "unknown"))
        model = str(f.get("model", "unknown"))
        base = {"provider": self._provider_label, "model": model, "role": role}
        total = int(f.get("usage") or 0)
        prompt = int(f.get("usage_prompt") or 0)
        completion = int(f.get("usage_completion") or 0)
        if prompt:
            self._tokens.add(prompt, {**base, "type": "input"})
        if completion:
            self._tokens.add(completion, {**base, "type": "output"})
        if total and not (prompt or completion):  # split unavailable
            self._tokens.add(total, {**base, "type": "total"})
        latency = float(f.get("latency_ms") or 0.0)
        self._llm_duration.record(latency, base)
        self._retro_span(
            "crucible.llm", f.get("subtask_id"), latency,
            {**base, "tokens.total": total,
             "tool_calls": int(f.get("tool_calls_count") or 0)},
        )

    def tool_call(self, **f: Any) -> None:
        name = str(f.get("name", "unknown"))
        ok = f.get("error") is None
        latency = float(f.get("latency_ms") or 0.0)
        self._tools.add(1, {"tool": name, "ok": str(ok).lower()})
        self._tool_duration.record(latency, {"tool": name})
        self._retro_span(
            "crucible.tool", f.get("subtask_id"), latency,
            {"tool.name": name, "ok": ok},
        )

    def critic_score(self, **f: Any) -> None:
        score = f.get("score")
        if score is not None:
            self._critic_scores.record(
                float(score), {"approved": str(bool(f.get("approved"))).lower()}
            )

    def verify(self, **f: Any) -> None:
        self._verifies.add(1, {"passed": str(bool(f.get("passed"))).lower()})

    # -- lifecycle -----------------------------------------------------------------
    def flush(self) -> None:
        # Engine calls this in its run-finally: push points now so short runs
        # aren't lost to the export interval.
        try:
            self._meter_provider.force_flush()
            if hasattr(self._tracer_provider, "force_flush"):
                self._tracer_provider.force_flush()
        except Exception:  # noqa: BLE001 - telemetry must never break a run
            pass

    def close(self) -> None:
        try:
            self._meter_provider.shutdown()
            if hasattr(self._tracer_provider, "shutdown"):
                self._tracer_provider.shutdown()
        except Exception:  # noqa: BLE001
            pass


def create_otel_observer(
    settings: TelemetrySettings | None = None,
    provider_label: str = "unknown",
) -> OTelObserver | None:
    """The composition-root entry point: None when disabled (callers chain it
    with MultiObserver only when present — zero OTel code runs otherwise)."""
    settings = settings or TelemetrySettings.load()
    if not settings.enabled:
        return None
    return OTelObserver(settings, provider_label=provider_label)
