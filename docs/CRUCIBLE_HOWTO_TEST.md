# How to test & run Crucible

A how-to for driving the `orchestrator` package (project name: **Crucible**) —
hermetic suite, in-process fake pipeline, real models, the TUI, and the example
task briefs you can reuse. Every symbol / env var / command below is grounded in
the repo; citations point at the file. All commands run from the workspace root.

---

## 1. Run the hermetic suite

The three commands:

```bash
uv sync                 # install deps (pyproject.toml; Python >=3.12)
uv run pytest -q        # the suite
uv run ruff check .     # lint
```

**What you'll see** (captured verbatim in `docs/CRUCIBLE_BASELINE.md`):

- `uv run pytest -q` → **286 passed**, exit `0`. (`docs/CRUCIBLE_BASELINE.md`,
  "Pytest".) `pyproject.toml [tool.pytest.ini_options]` sets `asyncio_mode = "auto"`
  and `testpaths = ["tests"]`, so bare `uv run pytest` works too.
- `uv run ruff check .` → **6 pre-existing errors**, exit `1` — e.g. `F841`
  `last_plan` in `orchestrator/orchestrator.py:184`, `E741` in
  `tests/test_e2e_fixture.py:101` and `tests/test_observers.py`. These predate
  this doc. (`docs/CRUCIBLE_BASELINE.md`, "Ruff lint".)

> The README describes the suite as "202 hermetic tests (injected LLM / verifier
> / clock — no live-LLM tests)" (`README.md`, Status section). The captured run
> above (286) is the authoritative current count.

**Why it's hermetic.** The orchestrator depends only on the `LLMClient` protocol
(`orchestrator/llm.py`); tests substitute `FakeLLMClient`, which is
"deterministic, network-free" and "so no unit test ever hits the network
(§12: no live LLM tests)" (`orchestrator/llm.py` module docstring + class
docstring). The ground-truth gate is also injected, not hard-wired:
`Orchestrator.__init__(llm, config, verifier=None)` takes a
`Verifier = Callable[[str], Awaitable[VerifyResult]]` (`orchestrator/orchestrator.py`),
so a test can hand in a fake verifier (or omit it, leaving `report.verified`
`None`). A suite-wide guard stops tests from ever reaching the developer's real
config: `tests/conftest.py` defines an **autouse** fixture `_isolate_crucible_config`
that `monkeypatch.setenv("CRUCIBLE_CONFIG", <tmp>/"no-such-config.json")` and
`monkeypatch.delenv`s `CRUCIBLE_OTEL`, `CRUCIBLE_OTEL_ENDPOINT`, `CRUCIBLE_OTEL_ENV`,
`CRUCIBLE_OTEL_TRACES` — so no real OTLP exporter is ever spun up.

**The per-role scripting pattern** (`tests/test_e2e_fixture.py`). `FakeLLMClient`
detects the active role from the `_role` marker the orchestrator injects into the
system message, falling back to `"default"` (`FakeLLMClient.role_of`). The most
flexible scripting form is a callable `(role, messages) -> LLMResponse` that
branches on role **and** on how many tool results have come back:

```python
from orchestrator.llm import FakeLLMClient, ToolCall, llm_response

def _count_tool_msgs(messages):
    return sum(1 for m in messages if m.get("role") == "tool")

def _script(role, messages):
    tool_msgs = _count_tool_msgs(messages)
    if role == "planner":
        return llm_response('{"reasoning": "...", "subtasks": ['
                            '  {"id": "r1", "role": "researcher", "task": "...", '
                            '   "depends_on": [], "inputs": ""},'
                            '  {"id": "c1", "role": "coder", "task": "...", '
                            '   "depends_on": ["r1"], "inputs": "..."}'
                            ']}')
    if role == "researcher":
        if tool_msgs == 0:                 # first turn: ask to read a file
            return llm_response(tool_calls=[ToolCall("t1", "read_file", {"path": "config.env"})])
        return llm_response('{"summary": "...", "confidence": 0.9}')  # then report
    if role == "coder":
        if tool_msgs == 0:                 # write the migration
            return llm_response(tool_calls=[ToolCall("w1", "write_file", {"path": "...", "content": "..."})])
        if tool_msgs == 1:                 # then verify via the sandboxed shell
            return llm_response(tool_calls=[ToolCall("s1", "run_shell", {"command": "grep ... "})])
        return llm_response('{"summary": "migrated", "confidence": 0.9}')
    if role == "synthesizer":
        return llm_response('{"summary": "done", "confidence": 0.9}')
    return llm_response('{"score": 9, "approved": true, "issues": [], '
                        '"suggestions": [], "rubric": []}')   # critic
```

The dict form (`{"role": [llm_response(...), ...]}`, returning items in order then
repeating the last) also works (`FakeLLMClient` constructor). Recordings are kept
on `fake.calls` (`list[dict]` with `role`/`messages`/`temperature`).

---

## 2. In-process fake pipeline

A minimal run mirrors `tests/test_e2e_fixture.py`: build a small v1 fixture in a
tmp workspace, drive a scripted `FakeLLMClient`, attach a `JSONLObserver`, then
inspect the returned `FinalReport`. Imports come straight from the public surface
(`orchestrator/__init__.py` `__all__`).

```python
import asyncio, io, json, tempfile
from pathlib import Path

from orchestrator import Config, Orchestrator
from orchestrator.llm import FakeLLMClient, ToolCall, llm_response
from orchestrator.observers import JSONLObserver

# 1. plant a tiny workspace (v1 token format), like the e2e fixture.
ws = Path(tempfile.mkdtemp())
(ws / "tokens.py").write_text("TOKEN_VERSION = 1\n")

# 2. script the fake by role + tool-message count.
def _script(role, messages):
    tool_msgs = sum(1 for m in messages if m.get("role") == "tool")
    if role == "planner":
        return llm_response(json.dumps({
            "reasoning": "migrate then verify",
            "subtasks": [
                {"id": "c1", "role": "coder",
                 "task": "migrate token format v1 to v2 in tokens.py and verify",
                 "depends_on": [], "inputs": ""},
            ],
        }))
    if role == "coder":
        if tool_msgs == 0:
            return llm_response(tool_calls=[ToolCall("w", "write_file", {
                "path": "tokens.py", "content": "TOKEN_VERSION = 2\n"})])
        if tool_msgs == 1:
            return llm_response(tool_calls=[ToolCall("s", "run_shell",
                                                     {"command": "grep VERSION tokens.py"})])
        return llm_response('{"summary": "migrated to v2", "confidence": 0.9}')
    if role == "synthesizer":
        return llm_response('{"summary": "Migration complete.", "confidence": 0.9}')
    return llm_response('{"score": 9, "approved": true, "issues": [], "suggestions": [], "rubric": []}')

async def main():
    buf = io.StringIO()
    config = Config(workspace=str(ws), observer=JSONLObserver(stream=buf))   # JSONL trace in-memory
    report = await Orchestrator(FakeLLMClient(_script), config).run(
        "migrate tokens from v1 to v2 and verify"
    )

    # 3. inspect the report (FinalReport, orchestrator/models.py):
    print(report.summary)          # str
    print(report.confidence)       # float (capped to 0.1 = DEGRADED_CONFIDENCE on budget/verify fail)
    print(report.tokens_total)     # int
    print(report.verified)         # bool | None  (None = no verifier injected; this run had none)
    print(report.iterations)       # int (critic revisions)
    for sid, res in report.subtask_results.items():          # dict[str, WorkerResult]
        print(sid, res.summary, res.artifacts, res.confidence)   # .uncertainties too
    for sid, sc in report.critic_scores.items():             # dict[str, CriticScore]
        print(sid, sc.score, sc.approved, sc.issues)         # .suggestions / .rubric too

    # 4. read the trace back (one JSON object per line), e.g. assert ordering:
    events = [json.loads(line)["event"] for line in buf.getvalue().splitlines() if line.strip()]
    assert events[0] == "run_started" and events[-1] == "run_finished"

asyncio.run(main())
```

The trace events a `JSONLObserver` emits (from `EVENTS` in
`orchestrator/observers.py`): `run_started`, `plan_ready`, `subtask_started`,
`llm_call`, `tool_call`, `critic_score`, `subtask_finished`, `verify`,
`run_finished`, plus lifecycle `flush`/`close`. Constructor options:
`JSONLObserver(stream=...)` writes to a text stream; `JSONLObserver(path=...)`
opens a file append-mode and owns the handle (`flush()` persists without closing;
`close()` releases only an owned handle, idempotent). Preview fields
(`messages_preview`, `output_preview`, `args`, `result_preview`, `task`) are
redacted by default. With no observer, `Config.observer=None` and
`select_observer` still default to JSONL → stdout, so a trace always exists.

For a real model instead of the fake, swap `FakeLLMClient(_script)` for a real
client built via the provider registry — see §3.

---

## 3. Real models

### Headless: `run.py`

`run.py` is **not** an installed console script — drive it directly
(README "Out of scope" lists "a CLI entry point" as out of scope). Its module
docstring gives the two shapes:

```bash
# task as an argument, project dir via --workspace (REQUIRED):
uv run python run.py --workspace ./my_project "migrate tokens v1->v2 and run pytest"

# or feed the task on stdin:
echo "summarize what auth.py does" | uv run python run.py --workspace ./my_project
```

Flags (from `run.py parse_args`):

| flag | meaning |
| --- | --- |
| `--workspace, -w <dir>` | **required** — sandboxed project dir the agents operate in |
| `--log <file>` | write the JSONL trace to a file instead of stdout |
| `--read-root <dir>` | repeatable; extra dir agents may **read** outside the workspace (writes/shell stay inside) |
| `--threshold <0-10>` | override `approval_threshold` |
| `--max-iterations <n>` | critic retry budget per subtask (`max_iterations_per_subtask`) |

`run.py` calls `resolve_from_env()` (see below), prints
`provider=<label> model=<model>` to stderr, builds the client with `make_client`,
wires a `JSONLObserver`, optionally chains an OTel observer when
`create_otel_observer(...)` returns non-`None` (i.e. `CRUCIBLE_OTEL=1`), then
`await Orchestrator(llm, config).run(task)` and prints the final
`CONFIDENCE / iterations / tokens / summary`. If nothing is configured it prints
`error: no provider configured …` and returns `2`.

### The explicit bring-your-own path: `LLMSettings.from_env`

The legacy escape hatch, used by the README Quickstart and re-exported from
`orchestrator/__init__.py`. `LLMSettings.from_env()` (`orchestrator/config.py`)
reads exactly three env vars:

| env var | field | default |
| --- | --- | --- |
| `LLM_API_KEY` | `api_key` | `None` |
| `LLM_BASE_URL` | `base_url` | `None` |
| `LLM_MODEL` | `model` | `"default"` |

…then you build a client: `OpenAIClient(s.api_key, s.base_url, s.model)` (targets
any OpenAI-compatible endpoint).

### Provider registry & auto-detection order

`orchestrator/providers.py` defines `PROVIDER_ORDER = ("glm", "minimax",
"anthropic", "openai")` and `PROVIDERS` (each a `Provider` with
`api_key_envs` checked **in order**, `base_url`, `default_model`, `kind`):

| Provider | `api_key_envs` (checked in order) | `base_url` | `default_model` | kind |
| --- | --- | --- | --- | --- |
| glm | `GLM_API_KEY`, `ZAI_API_KEY`, `Z_AI_API_KEY` | `https://api.z.ai/api/coding/paas/v4` | `glm-5.2` | openai |
| minimax | `MINIMAX_API_KEY` | `https://api.minimax.io/v1` | `MiniMax-M3` | openai |
| anthropic | `ANTHROPIC_API_KEY` | `None` (native SDK) | `claude-opus-4-8` | anthropic |
| openai | `OPENAI_API_KEY` | `https://api.openai.com/v1` | `""` (must set explicitly via `/model openai <name>`) | openai |

`resolve_from_env()` (`orchestrator/providers.py`) applies this precedence
verbatim (the module docstring states it):

1. If `LLM_API_KEY` is set → the explicit escape hatch:
   `ResolvedProvider(provider=None, api_key=LLM_API_KEY, base_url=LLM_BASE_URL,
   model=LLM_MODEL or "default")`; `label == "custom"`.
2. Otherwise the **first** provider in `PROVIDER_ORDER` whose key env is set,
   taking `model = LLM_MODEL` (if set) **else** that provider's `default_model`.
3. If nothing is set → returns `None` (the "no provider configured" error above).

`make_client(resolved)` returns an `AnthropicClient` when the provider's
`kind == "anthropic"` (native SDK), else an `OpenAIClient`.

### Copy-paste blocks

**GLM via the registry** (auto-detected, default model `glm-5.2`):

```bash
export GLM_API_KEY="..."        # or ZAI_API_KEY / Z_AI_API_KEY — same provider
# auto-resolves to glm-5.2 on https://api.z.ai/api/coding/paas/v4
uv run python run.py --workspace ./my_project "migrate tokens v1->v2 and run pytest"
```

**MiniMax via the registry** (auto-detected, default model `MiniMax-M3`):

```bash
export MINIMAX_API_KEY="..."
# auto-resolves to MiniMax-M3 on https://api.minimax.io/v1
uv run python run.py --workspace ./my_project "migrate tokens v1->v2 and run pytest"
```

**MiniMax via the explicit LLM_* hatch** (the README Quickstart's exact block;
lets you name any model, here `MiniMax-Text-01`):

```bash
export LLM_API_KEY="your-minimax-key"
export LLM_BASE_URL="https://api.minimax.io/v1"   # OpenAI-compatible endpoint
export LLM_MODEL="MiniMax-Text-01"
uv run python run.py --workspace ./my_project "migrate tokens v1->v2 and run pytest"
```

Note `LLM_API_KEY` always wins over the registry (it's checked first), so this
hatch overrides a set `GLM_API_KEY`/`MINIMAX_API_KEY`. The README's Python
Quickstart builds the same thing by hand: `s = LLMSettings.from_env()` then
`OpenAIClient(s.api_key, s.base_url, s.model)`.

---

## 4. TUI & swarm

The full-screen Textual app has an installed console script. `pyproject.toml
[project.scripts]`:

```
crucible = "orchestrator.tui.app:main"
```

`main()` (`orchestrator/tui/app.py`) parses `--workspace/-w` (default `.`),
`--theme`, `--no-splash`, then runs `CrucibleApp(...)`:

```bash
uv run crucible --workspace ./my_project [--theme <name>] [--no-splash]
```

(System-wide install without `uv run`: `uv tool install --editable .` — README,
Install.) On launch the banner prints the auto-detected
`provider=<label> model=<model> endpoint=<base_url|native>`; with no key set it
instructs you to export one of `GLM_API_KEY / MINIMAX_API_KEY /
ANTHROPIC_API_KEY / OPENAI_API_KEY` (or `LLM_API_KEY + LLM_BASE_URL`).

**Plain input vs `/swarm`.** Plain (non-`/`) input is a **conversation with your
operator** — a representative agent (`Operator`, `orchestrator/operator.py`) that
grounds itself in the workspace via read-only tools, composes a self-contained
task brief, and dispatches the swarm through its `run_swarm` tool. `/swarm <task>`
bypasses the operator for classic batch dispatch (the "swarm" branch of
`CrucibleApp._dispatch`). In `ask` permission mode, swarm dispatch itself prompts
for confirmation with the brief shown — a free plan-approval step
(`app.py _launch` / `_run_swarm`).

**Verified slash-command list** (single source: the `COMMANDS` dict in
`orchestrator/tui/commands.py`):

| command | does |
| --- | --- |
| `/help` | show the command list |
| `/permissions [auto\|ask\|readonly] \| allow <tool> \| reset` | show/set tool permission mode / session allowlist |
| `/theme [name]` | list themes or switch to one |
| `/model [provider] [model]` | show providers/keys, switch provider, or set the swarm model |
| `/rep [provider] [model]` | show or set the operator's model (fresh session) |
| `/swarm <task>` | dispatch the swarm directly, bypassing the operator |
| `/reset` | start a fresh operator conversation |
| `/tokens` | token / call / budget utilization for this session |
| `/plan` | re-show the current run's plan |
| `/copy [reply\|report\|all]` | copy last rep reply / final report / whole transcript |
| `/clear` | clear the transcript |
| `/workspace [dir]` | show or change the sandboxed workspace |
| `/roots [add <dir> \| rm <dir> \| clear]` | read-only paths agents may access OUTSIDE the workspace |
| `/threshold [0-10]` | show or set the critic approval threshold |
| `/quit` | exit crucible |

Aliases (`_ALIASES`, same file): `exit`→`quit`, `q`→`quit`, `perms`→`permissions`,
`?`→`help`. `parse_input(text)` returns `None` for plain task text, a
`SlashCommand(name, args)` for `/...`, and raises `CommandError` on an unknown or
malformed command.

---

## 5. Fixture / example task briefs

Concrete task strings found in the repo, reuse verbatim or adapt:

- **`"migrate tokens from v1 to v2 and verify"`** — the canonical end-to-end
  fixture's `orch.run(...)` argument
  (`tests/test_e2e_fixture.py::test_e2e_full_trace_redacted_and_silent`).
  Plants `tokens.py` (`TOKEN_VERSION = 1`) and `config.env`
  (`API_KEY=sk-supersecret1234567890`); the scripted coder migrates to
  `TOKEN_VERSION = 2` and verifies with `run_shell grep`. Planner subtasks:
  `r1` (researcher, `task`: "read tokens.py and config.env") → `c1` (coder,
  `task`: "migrate token format v1 to v2 in tokens.py and verify").
- **`"migrate ./workspace from token format v1 to v2 and verify with pytest"`** —
  the README Quickstart's `Orchestrator.run(...)` argument (`README.md`,
  Quickstart (MiniMax)).
- **`"migrate this codebase from token format v1 to v2 and verify with tests"`**
  — the spec's purpose example (`SPEC.md` §1, line 13).
- **`"migrate fixture_repo from token format v1 to v2 defined in specs/v2.md and
  verify with pytest"`** — the spec's recommended end-to-end fixture task
  (`SPEC.md`, line 382).
- **`"migrate tokens v1->v2 and run pytest"`** — a `run.py` argument example
  (`run.py` module docstring).
- **`"summarize what auth.py does"`** — a `run.py` stdin example (`run.py`
  module docstring).

The README flags the biggest open gap as proving genericity on a second,
non-`musa` project (`README.md`, Roadmap). For exercising secret redaction,
mirror the fixture's planted secret `API_KEY=sk-supersecret1234567890` and assert
neither `supersecret` nor the full key appears in the JSONL trace while
`REDACTED` does (`tests/test_e2e_fixture.py`).
