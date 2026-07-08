"""The shared worker execution loop (SPEC §4, §4.1, §4.2).

One loop drives every tool-using role; only the system prompt, tool set, and
temperature differ (§4). Dependency outputs are injected as distilled
``summary``+``artifacts`` blocks (§4.1), preserving per-worker isolation
(§13.2). Accumulated tool results are trimmed to a context budget (§4.2).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .budget import Budget
from .config import Config
from .constants import MAX_DEP_CONTEXT_BYTES
from .json_extract import extract_worker_result
from .llm import LLMClient, LLMResponse
from .models import Subtask, WorkerResult
from .observers import Observer, safe
from .redaction import redact_preview
from .roles import system_prompt
from .tools.registry import execute_tool, tool_defs_for

_LOW_CONFIDENCE = 0.4  # below this, flag confidence to downstream workers (§4.1)


def render_user_task(
    subtask: Subtask,
    dep_results: dict[str, WorkerResult],
    cap: int = MAX_DEP_CONTEXT_BYTES,
) -> str:
    """Render the worker's initial user message (§4.1).

    Body = the subtask's task, followed by a "Context from prior steps" block
    with each upstream dependency's ``summary`` + ``artifacts`` (+``confidence``
    when degraded/low). The block is capped; lowest-confidence-then-oldest
    dependencies are dropped first to stay under ``cap``.
    """
    parts = [subtask.task.strip()]
    deps = [(dep_id, dep_results[dep_id]) for dep_id in subtask.depends_on if dep_id in dep_results]
    if subtask.inputs:
        parts.append(f"\nWhat you need from upstream: {subtask.inputs}")

    if deps:
        blocks: list[tuple[str, float, int]] = []  # (text, confidence, order)
        for order, (dep_id, res) in enumerate(deps):
            artifacts = json.dumps(res.artifacts, default=str)
            flag = ""
            if res.confidence <= _LOW_CONFIDENCE:
                flag = f" [LOW CONFIDENCE {res.confidence:.2f} — verify before trusting]"
            block = (
                f"### From `{dep_id}`{flag}\n"
                f"summary: {res.summary}\n"
                f"artifacts: {artifacts}"
            )
            blocks.append((block, res.confidence, order))

        # Drop lowest-confidence (tie: oldest) until under cap.
        def total_len(bs) -> int:
            return sum(len(b[0]) for b in bs)

        kept = list(blocks)
        while total_len(kept) > cap and len(kept) > 1:
            # remove worst: lowest confidence, then lowest order (oldest)
            worst = min(range(len(kept)), key=lambda i: (kept[i][1], -kept[i][2]))
            kept.pop(worst)
        # If still over cap with a single block, hard-truncate it.
        if total_len(kept) > cap and kept:
            t, c, o = kept[0]
            kept[0] = (t[:cap], c, o)

        ordered = sorted(kept, key=lambda b: b[2])
        parts.append("\nContext from prior steps:\n" + "\n\n".join(b[0] for b in ordered))

    return "\n".join(parts)


def workspace_orientation(workspace: str, max_entries: int = 80) -> str:
    """A short orientation block naming the path convention and the current
    workspace contents (§4.1 support).

    Small models otherwise guess non-existent paths (``workspace/``, ``/``,
    ``research/``) and burn their tool budget on errors. Giving them the actual
    relative file listing up front grounds every subsequent tool call.
    """
    ws = Path(os.path.realpath(workspace))
    files: list[str] = []
    truncated = False
    if ws.is_dir():
        for root, dirs, names in os.walk(ws):
            dirs[:] = [d for d in sorted(dirs) if d not in (".git", "__pycache__")]
            for n in sorted(names):
                files.append(str(Path(root, n).relative_to(ws)))
                if len(files) >= max_entries:
                    truncated = True
                    break
            if truncated:
                break
    listing = "\n".join(files) if files else "(empty)"
    if truncated:
        listing += f"\n… (listing truncated at {max_entries} entries)"
    return (
        "Workspace orientation: you operate INSIDE the workspace root. All tool "
        "paths are RELATIVE to it — read `PROJECT.md`, not `workspace/PROJECT.md`; "
        "list the root with path `.`. Absolute paths and `..` are rejected. "
        f"Current workspace contents:\n{listing}"
    )


@dataclass
class WorkerOutput:
    result: WorkerResult
    messages: list[dict]


def _system_message(role: str) -> dict:
    # `_role` marker lets the FakeLLM route scripts; the real client ignores it.
    return {"role": "system", "content": system_prompt(role), "_role": role}


def _assistant_tool_message(resp: LLMResponse) -> dict:
    return {
        "role": "assistant",
        "content": resp.content or "",
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.args)},
            }
            for tc in resp.tool_calls
        ],
    }


def enforce_context_budget(messages: list[dict], budget_chars: int) -> list[dict]:
    """Trim oldest *tool* results first; never the system prompt or the original
    task (the first user message) (§4.2)."""
    def size(ms) -> int:
        return sum(len(json.dumps(m, default=str)) for m in ms)

    if size(messages) <= budget_chars:
        return messages
    # Protect index 0 (system) and the first user message.
    protected = set()
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            protected.add(i)
        if m.get("role") == "user" and "first_user" not in protected:
            protected.add(i)
            protected.add("first_user")
            break
    protected.discard("first_user")

    trimmable = [i for i, m in enumerate(messages) if i not in protected and m.get("role") == "tool"]
    for i in trimmable:
        if size(messages) <= budget_chars:
            break
        messages[i] = {**messages[i], "content": "[trimmed: context budget]"}
    return messages


async def _run_loop(
    llm: LLMClient,
    role: str,
    subtask_id: str,
    messages: list[dict],
    config: Config,
    observer: Observer | None,
    budget: Budget | None,
) -> WorkerOutput:
    tools = tool_defs_for(role)
    temperature = config.temperature_for(role)
    last_content = ""
    # Approx chars-per-token of 4 to map the token budget to a byte ceiling.
    budget_chars = config.worker_context_budget * 4

    for _step in range(config.max_steps):
        resp = await llm.complete(messages, tools=tools or None, temperature=temperature)
        last_content = resp.content or last_content
        if budget is not None:
            budget.note_call(resp.usage.total_tokens)
        safe(
            observer, "llm_call",
            subtask_id=subtask_id, role=role, model=resp.model,
            output_preview=redact_preview(resp.content or ""),
            latency_ms=resp.latency_ms,
            usage=resp.usage.total_tokens,
            tool_calls_count=len(resp.tool_calls),
        )

        if not resp.tool_calls:
            return WorkerOutput(result=extract_worker_result(resp.content or ""), messages=messages)

        messages.append(_assistant_tool_message(resp))
        for tc in resp.tool_calls:
            tr = await execute_tool(tc.name, tc.args, config.workspace)
            safe(
                observer, "tool_call",
                subtask_id=subtask_id, name=tc.name, args=tc.args,
                result_preview=redact_preview(tr.content),
                latency_ms=tr.latency_ms, error=tr.error,
            )
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": tr.content})
        enforce_context_budget(messages, budget_chars)

    # max_steps hit: fall back to parsing the last assistant content (§4).
    return WorkerOutput(result=extract_worker_result(last_content), messages=messages)


async def run_worker(
    llm: LLMClient,
    role: str,
    subtask: Subtask,
    dep_results: dict[str, WorkerResult],
    config: Config,
    observer: Observer | None = None,
    budget: Budget | None = None,
) -> WorkerOutput:
    safe(
        observer, "subtask_started",
        subtask_id=subtask.id, role=role, task=subtask.task,
        dependency_ids=list(subtask.depends_on),
    )
    user_content = (
        workspace_orientation(config.workspace)
        + "\n\n"
        + render_user_task(subtask, dep_results)
    )
    messages = [
        _system_message(role),
        {"role": "user", "content": user_content},
    ]
    return await _run_loop(llm, role, subtask.id, messages, config, observer, budget)


async def revise_worker(
    llm: LLMClient,
    role: str,
    subtask: Subtask,
    prior_messages: list[dict],
    issues: list[str],
    suggestions: list[str],
    config: Config,
    observer: Observer | None = None,
    budget: Budget | None = None,
) -> WorkerOutput:
    """Re-run the worker keeping its prior context, with the critic's feedback
    appended (§6 ``worker.revise``)."""
    messages = list(prior_messages)
    feedback = (
        "A critic reviewed your result and did not accept it.\n"
        f"Issues: {issues}\nSuggestions: {suggestions}\n"
        "Produce an improved result addressing these. Respond with the JSON object only."
    )
    messages.append({"role": "user", "content": feedback})
    return await _run_loop(llm, role, subtask.id, messages, config, observer, budget)


async def continue_worker(
    llm: LLMClient,
    role: str,
    subtask_id: str,
    prior_messages: list[dict],
    feedback: str,
    config: Config,
    observer: Observer | None = None,
    budget: Budget | None = None,
) -> WorkerOutput:
    """Resume a worker's PRIOR message history with new feedback appended.

    Unlike ``run_worker`` (which cold-starts), this keeps the accumulated
    context so a multi-pass loop behaves like one persistent session rather
    than N amnesiac attempts — the key to debugging many interrelated failures
    (verify-repair loop)."""
    messages = list(prior_messages)
    messages.append({"role": "user", "content": feedback})
    return await _run_loop(llm, role, subtask_id, messages, config, observer, budget)
