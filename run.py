"""Tiny runner so you can drive the orchestrator without a TUI.

Usage:
    export LLM_API_KEY=...            # your MiniMax key
    export LLM_BASE_URL=https://api.minimax.io/v1
    export LLM_MODEL=MiniMax-Text-01

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
import sys

from orchestrator import Config, JSONLObserver, OpenAIClient, Orchestrator
from orchestrator.config import LLMSettings


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the agentic orchestrator on a project.")
    p.add_argument("task", nargs="*", help="the task / prompt (or pass it on stdin)")
    p.add_argument("--workspace", "-w", required=True,
                   help="project directory the agents operate in (sandboxed)")
    p.add_argument("--log", default=None,
                   help="write JSONL trace to this file instead of stdout")
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

    settings = LLMSettings.from_env()
    if not settings.api_key:
        print("error: set LLM_API_KEY (and LLM_BASE_URL / LLM_MODEL)", file=sys.stderr)
        return 2

    llm = OpenAIClient(settings.api_key, settings.base_url, settings.model)

    overrides: dict = {"observer": JSONLObserver(path=args.log)}
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
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
