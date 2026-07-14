"""The Operator: a conversational agent that represents the user in front of
the swarm (client layer — the core engine stays batch).

One persistent chat session. Per user message the operator either answers
directly (questions, scoping, small talk) or composes a self-contained task
brief and dispatches the orchestrator via its ``run_swarm`` tool. The
orchestrator is *a tool of the operator*; the ground-truth verify gate stays
between the operator and any claim of "done" — the operator interprets
reports, it never vouches.

Dependencies are injected (LLM client, swarm runner, permission gate,
observer), so the whole thing is hermetic to test and model-agnostic — the
rep can run on a big model while the swarm stays on small ones.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from .models import FinalReport
from .observers import Observer, safe
from .redaction import redact_preview
from .tools import files
from .worker import _assistant_tool_message

OPERATOR_ROLE = "operator"
DEFAULT_OPERATOR_MAX_STEPS = 12

# Given a task brief, run the swarm and return its report. Injected so tests
# never touch the engine and the TUI controls client/config construction.
SwarmRunner = Callable[[str], Awaitable[FinalReport]]
# Same shape as Config.tool_gate: (name, args) -> bool | (bool, reason).
ToolGate = Callable[[str, dict], Awaitable[Any]]

SYSTEM_PROMPT = """\
You are the OPERATOR of Crucible — the user's representative in front of a \
swarm of small-model agents (planner -> DAG of workers -> critics -> \
synthesizer -> verify gate). You converse; the swarm builds.

For each user message decide:
- Conversation (questions, scoping, opinions, results discussion): answer \
directly. No tools.
- Real work in the workspace (build, edit, migrate, fix, test): compose a \
task brief and call run_swarm. Use read_file / list_files first when you \
need to ground the brief in the actual code.

Brief-writing rules — this is your highest-leverage act:
- The swarm has NO memory of this conversation. The brief must be \
self-contained: paths, names, constraints, and acceptance criteria the user \
gave you.
- State the definition of done, including verification (e.g. "uv run ruff \
check . and uv run pytest -q must pass").
- For follow-up fixes, describe the CURRENT broken state and the desired \
state — not the conversation history.
- Do not prescribe process or subtask structure; the orchestrator plans its \
own DAG.

Reporting rules:
- verified=True from the tool result is the ONLY proof of green. Never claim \
success beyond what the report supports; surface failures plainly.
- Keep replies short and conversational. Lead with the outcome.
"""

TOOL_DEFS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "run_swarm",
            "description": (
                "Dispatch the orchestrator swarm on the workspace with a "
                "complete, self-contained task brief. Expensive: one call per "
                "unit of real work, never for questions you can answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "the full task brief (self-contained)",
                    }
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file inside the workspace (relative path).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files/directories at a relative path inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": [],
            },
        },
    },
]


def summarize_report(report: FinalReport) -> str:
    """The report as the operator's tool result — compact, verify-first."""
    verified = {True: "PASS", False: "FAIL", None: "not run"}[report.verified]
    lines = [
        f"verify gate: {verified}",
        f"confidence: {report.confidence:.2f}",
        f"tokens: {report.tokens_total}  critic iterations: {report.iterations}",
        "summary:",
        report.summary.strip(),
    ]
    worker_bits = [
        f"  - {sid}: {res.summary}"[:200]
        for sid, res in report.subtask_results.items()
    ]
    if worker_bits:
        lines.append("worker results:")
        lines.extend(worker_bits[:12])
    return "\n".join(lines)


class Operator:
    def __init__(
        self,
        llm: Any,  # LLMClient protocol
        workspace: str,
        swarm_runner: SwarmRunner,
        *,
        tool_gate: ToolGate | None = None,
        observer: Observer | None = None,
        on_dispatch: Callable[[str], None] | None = None,
        max_steps: int = DEFAULT_OPERATOR_MAX_STEPS,
        read_roots: list[str] | None = None,
    ) -> None:
        self.llm = llm
        self.workspace = workspace
        self.swarm_runner = swarm_runner
        # Shared MUTABLE list: the TUI's /roots edits apply mid-session.
        self.read_roots: list[str] = read_roots if read_roots is not None else []
        self.tool_gate = tool_gate
        self.observer = observer
        self.on_dispatch = on_dispatch  # UI hook: show the brief when dispatching
        self.max_steps = max_steps
        self.messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT, "_role": OPERATOR_ROLE}
        ]

    # -- session hygiene -----------------------------------------------------
    def _heal_dangling(self) -> None:
        """If the last turn was cancelled mid-tool-call, the session ends with
        an assistant message whose tool_calls never got results — invalid to
        replay. Backfill cancellation results so the session stays usable."""
        if not self.messages:
            return
        last = self.messages[-1]
        if last.get("role") == "assistant" and last.get("tool_calls"):
            for tc in last["tool_calls"]:
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": "ERROR: cancelled by the user before completion",
                    }
                )

    def reset(self) -> None:
        del self.messages[1:]  # keep the system prompt

    # -- tool dispatch ---------------------------------------------------------
    async def _gate_denial(self, name: str, args: dict) -> str | None:
        if self.tool_gate is None:
            return None
        try:
            verdict = await self.tool_gate(name, args)
        except Exception as exc:  # noqa: BLE001 - a broken gate must not crash chat
            return f"ERROR: permission gate failed ({exc}); tool call not executed"
        reason = None
        if isinstance(verdict, tuple):
            allowed, reason = verdict
        else:
            allowed = bool(verdict)
        if allowed:
            return None
        return "ERROR: denied by permission policy" + (f": {reason}" if reason else "")

    async def _execute(self, name: str, args: dict) -> str:
        denial = await self._gate_denial(name, args)
        if denial is not None:
            return denial
        if name == "run_swarm":
            task = str(args.get("task", "")).strip()
            if not task:
                return "ERROR: run_swarm requires a non-empty task brief"
            if self.on_dispatch is not None:
                self.on_dispatch(task)
            try:
                report = await self.swarm_runner(task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - report, don't crash the chat
                return f"ERROR: swarm run failed: {exc}"
            return summarize_report(report)
        if name == "read_file":
            return await asyncio.to_thread(
                files.read_file, self.workspace, args.get("path", ""),
                read_roots=tuple(self.read_roots),
            )
        if name == "list_files":
            return await asyncio.to_thread(
                files.list_files, self.workspace, args.get("path", "."),
                read_roots=tuple(self.read_roots),
            )
        return f"ERROR: unknown tool {name!r}"

    # -- the chat turn ----------------------------------------------------------
    async def chat(self, user_text: str) -> str:
        """One conversational turn. The session persists across calls."""
        self._heal_dangling()
        self.messages.append({"role": "user", "content": user_text})
        last_content = ""
        for _step in range(self.max_steps):
            resp = await self.llm.complete(self.messages, tools=TOOL_DEFS, temperature=0.2)
            last_content = resp.content or last_content
            safe(
                self.observer, "llm_call",
                role=OPERATOR_ROLE, model=resp.model,
                output_preview=redact_preview(resp.content or ""),
                latency_ms=resp.latency_ms, usage=resp.usage.total_tokens,
                usage_prompt=resp.usage.prompt_tokens,
                usage_completion=resp.usage.completion_tokens,
                tool_calls_count=len(resp.tool_calls),
            )
            if not resp.tool_calls:
                self.messages.append({"role": "assistant", "content": resp.content or ""})
                return resp.content or ""
            self.messages.append(_assistant_tool_message(resp))
            for tc in resp.tool_calls:
                result = await self._execute(tc.name, tc.args)
                safe(
                    self.observer, "tool_call",
                    subtask_id="operator", name=tc.name,
                    args={k: str(v)[:200] for k, v in tc.args.items()},
                    result_preview=redact_preview(result),
                    latency_ms=0.0,
                    error=result if result.startswith("ERROR") else None,
                )
                self.messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": result}
                )
        # step budget exhausted: return what we have rather than looping forever
        self.messages.append({"role": "assistant", "content": last_content})
        return last_content or "(operator hit its step budget without a final reply)"
