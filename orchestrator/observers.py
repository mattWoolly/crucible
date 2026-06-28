"""Pluggable, push-based observability (SPEC §9).

One method per event; observers are chained and swapped without touching the
orchestrator. All dispatch goes through ``safe()`` so a buggy observer never
aborts a run. The tracing observer redacts previews by default (§9).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, TextIO

from .redaction import redact_preview

# Event names (also the Observer method names).
EVENTS = (
    "run_started",
    "plan_ready",
    "subtask_started",
    "llm_call",
    "tool_call",
    "critic_score",
    "subtask_finished",
    "run_finished",
    "flush",
)


class Observer:
    """Base observer: every event is a no-op. Subclass and override."""

    def run_started(self, **f: Any) -> None: ...
    def plan_ready(self, **f: Any) -> None: ...
    def subtask_started(self, **f: Any) -> None: ...
    def llm_call(self, **f: Any) -> None: ...
    def tool_call(self, **f: Any) -> None: ...
    def critic_score(self, **f: Any) -> None: ...
    def subtask_finished(self, **f: Any) -> None: ...
    def run_finished(self, **f: Any) -> None: ...
    def flush(self) -> None: ...


def safe(observer: Observer | None, event: str, **fields: Any) -> None:
    """Dispatch an event, swallowing any observer exception (§9 robustness)."""
    if observer is None:
        return
    try:
        getattr(observer, event)(**fields)
    except Exception:  # noqa: BLE001 - a buggy observer must not abort a run
        pass


class NoopObserver(Observer):
    """Silent default (§9)."""


class JSONLObserver(Observer):
    """One JSON line per event, previews redacted (§9)."""

    def __init__(self, stream: TextIO | None = None, path: str | None = None) -> None:
        self._own = False
        if path:
            self._stream = open(path, "a", encoding="utf-8")
            self._own = True
        else:
            self._stream = stream or sys.stdout

    def _emit(self, event: str, fields: dict) -> None:
        record = {"event": event, **_redact_fields(fields)}
        self._stream.write(json.dumps(record, default=str) + "\n")
        self._stream.flush()

    def run_started(self, **f): self._emit("run_started", f)
    def plan_ready(self, **f): self._emit("plan_ready", f)
    def subtask_started(self, **f): self._emit("subtask_started", f)
    def llm_call(self, **f): self._emit("llm_call", f)
    def tool_call(self, **f): self._emit("tool_call", f)
    def critic_score(self, **f): self._emit("critic_score", f)
    def subtask_finished(self, **f): self._emit("subtask_finished", f)
    def run_finished(self, **f): self._emit("run_finished", f)

    def flush(self) -> None:
        try:
            self._stream.flush()
        finally:
            if self._own:
                self._stream.close()


# Preview fields that may carry secrets and must be redacted before leaving the process.
_PREVIEW_FIELDS = {"messages_preview", "output_preview", "args", "result_preview", "task"}


def _redact_fields(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k in _PREVIEW_FIELDS and isinstance(v, str):
            out[k] = redact_preview(v)
        elif k in _PREVIEW_FIELDS:
            from .redaction import redact
            out[k] = redact(v)
        else:
            out[k] = v
    return out


class LangfuseObserver(Observer):
    """Tracing-backend observer (§9). Redaction default-on; degrades to Noop if
    the package or credentials are unavailable so a missing backend never breaks
    a run."""

    def __init__(self, public_key=None, secret_key=None, host=None) -> None:
        self._ok = False
        try:  # pragma: no cover - optional dependency, exercised in integration only
            from langfuse import Langfuse  # type: ignore

            if public_key and secret_key:
                self._client = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
                self._ok = True
        except Exception:
            self._ok = False

    def _send(self, event: str, fields: dict) -> None:  # pragma: no cover
        if not self._ok:
            return
        self._client.event(name=event, metadata=_redact_fields(fields))

    def run_started(self, **f): self._send("run_started", f)
    def plan_ready(self, **f): self._send("plan_ready", f)
    def subtask_started(self, **f): self._send("subtask_started", f)
    def llm_call(self, **f): self._send("llm_call", f)
    def tool_call(self, **f): self._send("tool_call", f)
    def critic_score(self, **f): self._send("critic_score", f)
    def subtask_finished(self, **f): self._send("subtask_finished", f)
    def run_finished(self, **f): self._send("run_finished", f)

    def flush(self) -> None:  # pragma: no cover
        if self._ok:
            try:
                self._client.flush()
            except Exception:
                pass


def select_observer(explicit: Observer | None = None) -> Observer:
    """Auto-select (§9): explicit wins; else tracing creds -> Langfuse; else
    JSONL to OBSERVER_LOG_FILE or stdout."""
    if explicit is not None:
        return explicit
    pub = os.environ.get("OBSERVER_TRACING_PUBLIC_KEY")
    sec = os.environ.get("OBSERVER_TRACING_SECRET_KEY")
    if pub and sec:
        obs = LangfuseObserver(pub, sec, os.environ.get("OBSERVER_TRACING_HOST"))
        if getattr(obs, "_ok", False):
            return obs
    log_file = os.environ.get("OBSERVER_LOG_FILE")
    return JSONLObserver(path=log_file) if log_file else JSONLObserver()
