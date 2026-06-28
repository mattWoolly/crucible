# orchestrator

A minimal agentic orchestrator that completes complex tasks by decomposing them
into a validated DAG of critic-gated specialist sub-agents. Quality comes from
**structure** — decomposition, isolation, critique, verification — not from a
smarter model. Every worker can run on a small, cheap model (e.g. MiniMax).

Built to [`SPEC.md`](./SPEC.md). Implementation plan in
[`docs/superpowers/plans/`](./docs/superpowers/plans/).

## Install

```bash
uv sync
```

## Quickstart (MiniMax)

The real client targets any OpenAI-compatible endpoint. For MiniMax:

```bash
export LLM_API_KEY="your-minimax-key"
export LLM_BASE_URL="https://api.minimax.io/v1"   # OpenAI-compatible endpoint
export LLM_MODEL="MiniMax-Text-01"
```

```python
import asyncio
from orchestrator import Orchestrator, Config, OpenAIClient, LLMSettings

async def main():
    s = LLMSettings.from_env()
    llm = OpenAIClient(s.api_key, s.base_url, s.model)
    config = Config.from_env(workspace="./workspace")   # all paths sandboxed here
    report = await Orchestrator(llm, config).run(
        "migrate ./workspace from token format v1 to v2 and verify with pytest"
    )
    print(report.summary, report.confidence)

asyncio.run(main())
```

Tests never touch the network — they use `FakeLLMClient`:

```python
from orchestrator import Orchestrator, Config, FakeLLMClient
```

## How it works (SPEC map)

```
task → Planner → validate + augment → topological layers
     → workers (parallel, fault-isolated) → critic-gated per result
     → Synthesizer → Final Critic → FinalReport
```

- **Plan validation + augmentation** (§5): reject cycles/dangling deps, then
  always ensure a coder + synthesizer exist. The single most reliable feature.
- **Critic gating before propagation** (§6): an independent, rubric-driven
  critic scores each result *before* it can poison downstream workers; accepted
  revisions are kept (v2 fix), and a convergence guard stops stuck loops.
- **Final-answer verification** (§6.3): the merged answer is gated too.
- **Sandboxed tools** (§7): symlink-aware file containment; no-shell tokenized
  exec with metacharacter rejection, an allowlist, and argument guards on
  `find -exec` / `git -c` / `xargs`.
  **The sandbox is a speed bump, not a boundary — run untrusted tasks inside a
  container (§7.3).**
- **Observability** (§9): push-based Observer (`Noop`/`JSONL`/`Langfuse`),
  secret redaction on previews, authoritative flush in `finally`.
- **Bounded cost** (§10): global call/token/time ceilings degrade gracefully.

## Configuration

Knobs are `Config` constructor args (see `orchestrator/config.py`):
`approval_threshold` (+ per-role overrides), `max_iterations_per_subtask`,
`max_steps`, `worker_context_budget`, `max_llm_calls`, `max_total_tokens`,
`run_timeout_s`, `role_temperature`, `observer`.

## Tests

```bash
uv run pytest -q
```

## Out of scope

Cross-run memory, live LLM integration tests, a CLI entry point, multi-repo
tasks, a streaming UI, and provisioning the container — see SPEC §12.
