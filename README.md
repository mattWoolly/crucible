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
- **Full-screen TUI** (`uv run crucible`): live plan tree + transcript, slash
  commands (`/permissions`, `/theme`, `/model`, `/tokens`, …), interactive
  tool-permission gate, token utilization footer
- 202 hermetic tests (injected LLM / verifier / clock — no live-LLM tests)

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

To get the `crucible` command system-wide (any directory, no `uv run`):

```bash
uv tool install --editable .   # installs ~/.local/bin/crucible
```

`--editable` points the installed command at this working tree, so code
changes apply without reinstalling. Drop it for a frozen snapshot; after
dependency changes, `uv tool upgrade orchestrator` re-syncs the tool env.

## TUI

```bash
# provider auto-detected from whichever key is set (first found wins):
#   GLM_API_KEY (or ZAI_API_KEY) → glm-5.2 on z.ai
#   MINIMAX_API_KEY              → MiniMax-M3
#   ANTHROPIC_API_KEY            → claude-opus-4-8 (official anthropic SDK,
#                                  also resolves `ant auth login` profiles)
#   OPENAI_API_KEY               → needs an explicit /model openai <name>
# or force a custom endpoint: LLM_API_KEY + LLM_BASE_URL + LLM_MODEL
crucible --workspace ./my_project
```

Full-terminal app (Textual): splash logo, a live plan tree beside the event
transcript, an input prompt, and a status bar with token/call utilization.

**Plain input is a conversation with your operator** — a representative agent
that clarifies intent, grounds itself in the workspace (read-only tools),
composes a self-contained task brief, and dispatches the swarm via its
`run_swarm` tool. It interprets the report honestly: the verify gate is the
only proof of green, never the model's say-so. The operator defaults to the
biggest brain available (anthropic if a key/profile exists, else the swarm's
provider); the swarm stays on whatever `/model` says — big rep, cheap workers.
`/swarm <task>` bypasses the operator for classic batch dispatch. In `ask`
permission mode, swarm dispatch itself prompts for confirmation with the brief
shown — a free plan-approval step.

Slash commands:

| command | does |
| --- | --- |
| `/permissions [auto\|ask\|readonly]` | tool permission mode — `ask` (default) prompts before `write_file`/`run_shell`; `allow <tool>` / `reset` manage the session allowlist |
| `/theme [name]` | list or switch Textual themes |
| `/model [provider] [model]` | list providers + key status, switch provider, or set the swarm model |
| `/rep [provider] [model]` | show or change the operator's model (fresh session) |
| `/swarm <task>` | dispatch the swarm directly, bypassing the operator |
| `/reset` | fresh operator conversation |
| `/tokens` | session token / call / per-role utilization |
| `/roots [add <dir>\|rm <dir>\|clear]` | read-only dirs agents may access **outside** the workspace (e.g. `../sibling`) — writes and shell always stay inside |
| `/workspace [dir]`, `/threshold [0-10]`, `/plan`, `/clear`, `/quit` | the obvious |

`esc` cancels a run; `↑/↓` recall input history. The permission gate rides the
engine's `Config.tool_gate` hook — headless runs (no gate) behave exactly as
before.

Select/copy/paste: click-drag selects text in the transcript, `ctrl+c` copies
the selection (OSC 52 — works in WezTerm and most modern terminals; hold
Shift while dragging for terminal-native selection instead). `/copy` grabs
the last rep reply without any mouse work (`/copy report` / `/copy all` for
the last final report / whole transcript). Pasting into the prompt flattens
multi-line text into one line instead of dropping everything after line one.

## Telemetry (OpenTelemetry, opt-in)

Enable persistently in `~/.config/crucible/config.json` (path overridable
via `CRUCIBLE_CONFIG`):

```json
{ "otel": { "enabled": true } }
```

or per-process with `CRUCIBLE_OTEL=1`. Precedence: env > config file >
defaults (`CRUCIBLE_OTEL=0` force-disables even when the file enables).
Metrics + traces go to a local OTLP collector (gRPC, default
`http://localhost:4317` — override with `CRUCIBLE_OTEL_ENDPOINT` or the
file's `otel.endpoint`; `CRUCIBLE_OTEL_TRACES=0` / `otel.traces` disables
traces; `CRUCIBLE_OTEL_ENV` / `otel.environment` sets the `env` resource
label). Default **off**: zero OTel code runs. No prompt/response content is
exported — only counts, durations, model ids, tool names, roles, and
pass/fail outcomes.

Prometheus surface (per provider/model/role): `crucible_token_usage_total`
(`type=input|output`), `crucible_llm_duration_milliseconds_*`,
`crucible_run_count_total`, `crucible_tool_count_total` + duration,
`crucible_verify_count_total{passed}`, `crucible_critic_score_*`,
`crucible_subtask_duration_milliseconds_*`. Traces: `crucible.run` →
`crucible.subtask` → `crucible.llm`/`crucible.tool` spans (explicit
parenting; subtask ids are span attributes only, never metric labels).

Implementation: `orchestrator/otel.py`, an `Observer` chained via
`MultiObserver` — the engine is untouched; telemetry rides the same event
stream as the JSONL trace and the TUI.

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
