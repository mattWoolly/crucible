# Crucible

**A minimal agentic orchestrator that makes small, cheap models build real,
verified-green software.** It completes complex tasks by decomposing them into a
validated DAG of critic-gated specialist sub-agents. Quality comes from
**structure** — decomposition, isolation, critique, and ground-truth
verification — not from a smarter model. Every worker runs on a small model
(reproduced end-to-end on MiniMax-M3 and z.ai GLM-5.2).

> The Python package is `orchestrator`; **Crucible** is the project.

Built to [`SPEC.md`](./SPEC.md). The full evidence-driven evolution — every
mutation from "can it even call a tool?" to reproducibly-green multi-model
builds — is in [`docs/EVOLUTION.md`](./docs/EVOLUTION.md).

## Status — feature-complete v1

The core does what it was built to do, proven across two model families: a small
model autonomously builds a real ~50-file project and it passes its own
`ruff` + `pytest` gate, **verified from a clean checkout** (never the model's
say-so).

**What's in:**
- Plan → validated DAG → critic-gated parallel workers → synthesizer → final critic
- **Ground-truth verify gate** (real `ruff` + `pytest`, run independently) — the pivotal lever
- Bounded **verify-repair loop** with a persistent debugging session (batch or incremental)
- **Two-phase build→auto-repair** pipeline (build, then repair-continue to green)
- Resilience: rate-limit backoff, per-request timeout, crash-safe partial commits, graceful synthesis degradation
- Sandboxed shell tool: allowlist, quote-aware metacharacter guard, arg-guards, ad-hoc-install guard
- Reproducibility: source-level install guard **plus** clean-env verify (`--verify-isolated`, declared-deps only)
- **Model-agnostic** by dependency injection; a provider registry (MiniMax, z.ai GLM)
- Observability: JSONL trace with secret redaction and tail-kept verify output
- 154 hermetic tests (injected LLM / verifier / clock — no live-LLM tests)

**Proven:** green, clean-checkout-reproducible builds from **MiniMax-M3** and **z.ai GLM-5.2**.

**Roadmap (breadth & polish — not core capability):**
- Prove genericity on a second, non-`musa` project (the biggest open gap)
- Language-agnostic verifiers (the driver currently hardcodes `ruff`+`pytest`)
- Per-role model routing + token/cost accounting
- Run resumability (today a crash commits partial work but can't resume)

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
