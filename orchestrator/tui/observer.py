"""Observer → UI bridge (no Textual imports; hermetic to test).

``TUIObserver`` funnels every engine event into a single ``emit(event,
fields)`` callback. The app supplies a callback that posts a Textual message;
tests supply a list-appender. Event names mirror ``observers.EVENTS``.
"""

from __future__ import annotations

from typing import Any, Callable

from ..observers import Observer

Emit = Callable[[str, dict], None]


class TUIObserver(Observer):
    def __init__(self, emit: Emit) -> None:
        self._emit = emit

    def _send(self, event: str, fields: dict[str, Any]) -> None:
        # safe() already shields the engine from observer bugs; nothing extra.
        self._emit(event, fields)

    def run_started(self, **f: Any) -> None: self._send("run_started", f)
    def plan_ready(self, **f: Any) -> None: self._send("plan_ready", f)
    def subtask_started(self, **f: Any) -> None: self._send("subtask_started", f)
    def llm_call(self, **f: Any) -> None: self._send("llm_call", f)
    def tool_call(self, **f: Any) -> None: self._send("tool_call", f)
    def critic_score(self, **f: Any) -> None: self._send("critic_score", f)
    def subtask_finished(self, **f: Any) -> None: self._send("subtask_finished", f)
    def verify(self, **f: Any) -> None: self._send("verify", f)
    def run_finished(self, **f: Any) -> None: self._send("run_finished", f)
