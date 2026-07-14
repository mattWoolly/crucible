"""Tiny runner so you can drive the orchestrator without a TUI.

Usage:
    # provider auto-detected from GLM_API_KEY / MINIMAX_API_KEY /
    # ANTHROPIC_API_KEY / OPENAI_API_KEY (first found wins), or force a
    # custom endpoint with LLM_API_KEY + LLM_BASE_URL + LLM_MODEL.
    export GLM_API_KEY=...

    # task as an argument, project dir with --workspace:
    uv run python run.py --workspace ./my_project "migrate tokens v1->v2 and run pytest"

    # or pipe/type the task on stdin:
    echo "summarize what auth.py does" | uv run python run.py --workspace ./my_project

Progress streams as JSON lines (set --log to write them to a file instead of
stdout). The final answer is printed at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from orchestrator import Config, JSONLObserver, Orchestrator
from orchestrator.observers import MultiObserver
from orchestrator.otel import create_otel_observer
from orchestrator.providers import make_client, resolve_from_env


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the agentic orchestrator on a project.")
    p.add_argument("task", nargs="*", help="the task / prompt (or pass it on stdin)")
    p.add_argument("--workspace", "-w", required=True,
                   help="project directory the agents operate in (sandboxed)")
    p.add_argument("--log", default=None,
                   help="write JSONL trace to this file instead of stdout")
    p.add_argument("--read-root", action="append", default=[],
                   help="extra directory agents may READ (repeatable); writes stay in the workspace")
    p.add_argument("--threshold", type=float, default=None,
                   help="override approval_threshold (0-10)")
    p.add_argument("--max-iterations", type=int, default=None,
                   help="critic retry budget per subtask")
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    task = " ".join(args.task).strip() or sys.stdin.read().strip()
    if not task:
        print("error: no task given (argument or stdin)", file=sys.stderr)
        return 2

    backend = resolve_from_env()
    if backend is None:
        print("error: no provider configured — set GLM_API_KEY / MINIMAX_API_KEY / "
              "ANTHROPIC_API_KEY / OPENAI_API_KEY, or LLM_API_KEY (+ LLM_BASE_URL / LLM_MODEL)",
              file=sys.stderr)
        return 2
    print(f"provider={backend.label} model={backend.model}", file=sys.stderr)

    llm = make_client(backend)

    observer = JSONLObserver(path=args.log)
    otel = create_otel_observer(provider_label=backend.label)  # None unless CRUCIBLE_OTEL=1
    if otel is not None:
        observer = MultiObserver([observer, otel])
        print("otel: exporting to local collector", file=sys.stderr)

    overrides: dict = {"observer": observer}
    if args.read_root:
        overrides["read_roots"] = tuple(os.path.abspath(r) for r in args.read_root)
    if args.threshold is not None:
        overrides["approval_threshold"] = args.threshold
    if args.max_iterations is not None:
        overrides["max_iterations_per_subtask"] = args.max_iterations
    config = Config.from_env(workspace=args.workspace, **overrides)

    report = await Orchestrator(llm, config).run(task)

    print("\n" + "=" * 70)
    print(f"CONFIDENCE: {report.confidence:.2f}   "
          f"(iterations={report.iterations}, tokens={report.tokens_total})")
    print("=" * 70)
    print(report.summary)
    if otel is not None:
        otel.close()  # headless runs are short — force the final export
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
