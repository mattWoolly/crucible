# Minimal Agentic Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Python async agentic harness described in `SPEC.md` — a small-model orchestrator that decomposes a task into a validated DAG, runs critic-gated specialist workers in topological layers, synthesizes and verifies a final answer, all observable and budget-bounded.

**Architecture:** A single shared worker execution loop drives every role (planner/researcher/coder/critic/synthesizer); only the system prompt, tool set, and temperature differ. The orchestrator validates+augments the planner's DAG, runs each topological layer in parallel with fault isolation, gates every result through an independent critic before it propagates, then synthesizes and re-verifies. An OpenAI-compatible `LLMClient` (MiniMax) is hidden behind a protocol so tests use an in-repo fake. Everything emits through a pluggable, redacting Observer.

**Tech Stack:** Python 3.12, asyncio, pydantic v2 (schema-validated outputs), `openai` SDK (MiniMax OpenAI-compatible endpoint), pytest + pytest-asyncio. Managed with `uv`.

## Global Constraints

- Python >= 3.12; async throughout (`asyncio.gather(..., return_exceptions=True)` for layers).
- LLM access is OpenAI-compatible via env `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`. Real client uses `openai`; **all tests use `FakeLLMClient`** — no network in unit tests (SPEC §12: no live LLM tests).
- `DEGRADED_CONFIDENCE = 0.1` is a **single named constant** used everywhere a degraded result is built or routed on (SPEC §8, §11).
- Every role except `planner` outputs JSON parsed by the layered extractor (SPEC §8); parse failure returns a degraded result, never throws.
- The in-process sandbox is a speed bump, not a boundary (SPEC §7.3) — code comments must say so; no claim of adversarial containment.
- Observer calls are wrapped so a buggy observer never aborts a run (SPEC §9). Authoritative flush in `finally` (SPEC §9, §11).
- Iteration/index updates are **positional**, never value-equality (SPEC §6.4).
- Package name: `orchestrator`. No CLI entry point, no cross-run memory, no live integration tests (SPEC §12).

---

## File Structure

| File | Responsibility |
| --- | --- |
| `orchestrator/constants.py` | `DEGRADED_CONFIDENCE`, role name constants, default knob values. |
| `orchestrator/errors.py` | Exception types: `PlanValidationError`, `BudgetExhausted`, `LLMError`. |
| `orchestrator/models.py` | pydantic models: `Subtask`, `Plan`, `WorkerResult`, `RubricItem`, `CriticScore`, `FinalReport`. |
| `orchestrator/json_extract.py` | Layered JSON extraction + degraded fallback (§8). |
| `orchestrator/config.py` | `Config` dataclass (knobs §10), env-driven construction. |
| `orchestrator/llm.py` | `LLMClient` protocol, `LLMResponse`, `OpenAIClient` (retry/backoff), `FakeLLMClient`. |
| `orchestrator/roles.py` | Per-role system prompt, rubric text, tool names, temperature (§3, §6.1). |
| `orchestrator/tools/files.py` | `read_file`, `write_file`, `list_files` + path containment (§7). |
| `orchestrator/tools/shell.py` | `run_shell` with layered guards (§7 shell). |
| `orchestrator/tools/registry.py` | Tool JSON-schema defs, `execute_tool`, per-role tool sets. |
| `orchestrator/redaction.py` | Secret/pattern scrubbing for previews (§9). |
| `orchestrator/observers.py` | `Observer` base, `Noop`/`JSONL`/`Langfuse`, safe-dispatch, auto-select (§9). |
| `orchestrator/plan_validation.py` | Schema + DAG validation + topological layering (§5.1). |
| `orchestrator/augmentation.py` | Idempotent coder/synthesizer injection (§5.2). |
| `orchestrator/worker.py` | Shared execution loop, dependency rendering (§4.1), context budget (§4.2). |
| `orchestrator/critic.py` | Critic call, threshold gate, critic loop + convergence guard (§6). |
| `orchestrator/synthesizer.py` | Synthesis + final-answer critic pass (§6.3). |
| `orchestrator/orchestrator.py` | `Orchestrator.run()`: plan→validate→augment→layered exec→synth→report; budgets, error handling (§2, §10, §11). |
| `orchestrator/__init__.py` | Public exports. |

Tests mirror these under `tests/`.

---

### Task 1: Constants, errors, and core models

**Files:** Create `orchestrator/constants.py`, `orchestrator/errors.py`, `orchestrator/models.py`; Test `tests/test_models.py`.

**Produces:**
- `DEGRADED_CONFIDENCE = 0.1`; role constants `PLANNER/RESEARCHER/CODER/CRITIC/SYNTHESIZER`; `WORKER_ROLES`, `ALLOWED_PLAN_ROLES`.
- `Subtask(id:str, role:str, task:str, depends_on:list[str]=[], inputs:str="")`.
- `Plan(reasoning:str="", subtasks:list[Subtask])`.
- `WorkerResult(summary:str, artifacts:dict=...,  confidence:float, uncertainties:list[str]=[])`; classmethod `degraded(reason, raw)`.
- `RubricItem(criterion:str, passed:bool, note:str="")`; `CriticScore(score:float, approved:bool, issues:list[str]=[], suggestions:list[str]=[], rubric:list[RubricItem]=[])`; classmethod `failed_validation()` → score 5, approved False, issue "critic output failed validation".
- `FinalReport(summary, confidence, subtask_results:dict[str,WorkerResult], critic_scores:dict, iterations:int, tokens_total:int)`.

Tests: degraded() sets confidence==DEGRADED_CONFIDENCE; CriticScore.failed_validation defaults; model JSON round-trips.

### Task 2: Layered JSON extraction (§8)

**Files:** Create `orchestrator/json_extract.py`; Test `tests/test_json_extract.py`.

**Produces:** `extract_json(text:str) -> dict` and `extract_worker_result(text:str) -> WorkerResult`.

Layers in order: strip code fences → `json.loads` → `raw_decode` scan keeping **largest balanced `{}`** (tie→last) → invalid-escape sanitization (`\x`→`\\x` for non-valid escapes) → degraded `WorkerResult` with `confidence=DEGRADED_CONFIDENCE`, `uncertainties=["parse_failed: ..."]`, summary = first 500 chars.

Tests: plain object; ```json fenced; prose-with-inline-example-then-real-object picks largest/last not first; trailing text; invalid escape `\d`; total garbage → degraded with DEGRADED_CONFIDENCE.

### Task 3: Config + env (§10)

**Files:** Create `orchestrator/config.py`; Test `tests/test_config.py`.

**Produces:** `Config` dataclass with all §10 knobs and defaults (`approval_threshold=8`, `approval_threshold_by_role={}`, `max_iterations_per_subtask=1`, `max_steps=8`, `worker_context_budget=120_000`, `max_llm_calls=200`, `max_total_tokens=None`, `run_timeout_s=600`, `role_temperature={planner:0.0,critic:0.0,default:0.2}`, `workspace`, `observer=None`). `threshold_for(role)` returns per-role override or base. `temperature_for(role)`. `Config.from_env(workspace, **overrides)`.

Tests: defaults; per-role threshold override; temperature_for falls back to default; from_env reads LLM_MODEL.

### Task 4: LLM client protocol + fake (MiniMax OpenAI-compatible)

**Files:** Create `orchestrator/llm.py`; Test `tests/test_llm.py`.

**Produces:**
- `@dataclass LLMResponse(content:str, tool_calls:list[ToolCall], usage:Usage, latency_ms:float, model:str)`; `ToolCall(id, name, args:dict)`; `Usage(prompt_tokens, completion_tokens, total_tokens)`.
- `class LLMClient(Protocol): async def complete(self, messages, tools=None, temperature=0.0) -> LLMResponse`.
- `OpenAIClient` wrapping `openai.AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)`; retry N times with exponential backoff on network/5xx; raises `LLMError` after exhaustion. Parses tool_calls (arguments JSON via `extract_json`).
- `FakeLLMClient(scripts: dict[role,list[LLMResponse]] | callable)` — deterministic, records calls, supports scripting tool-call then final-answer turns per role.

Tests (fake only): returns scripted response per role/turn; records call count; tool_call parsing shape. OpenAIClient retry/backoff tested by injecting a fake async transport that raises then succeeds (monkeypatched client) — no real network.

### Task 5: File tools + containment (§7 file)

**Files:** Create `orchestrator/tools/files.py`; Test `tests/tools/test_files.py`.

**Produces:** `resolve_in_workspace(workspace, path) -> Path` (symlink+`..`-aware, raises on escape); `read_file(workspace, path, max_bytes=200_000)`, `write_file(workspace, path, content)`, `list_files(workspace, path=".")`. Each returns a string result; failures return error strings (not raises) for the worker loop.

Tests: read within workspace; `..` escape rejected; symlink escape rejected; absolute path escape rejected; size cap truncates/flags; write then read round-trip; list_files.

### Task 6: Shell tool guards (§7 shell)

**Files:** Create `orchestrator/tools/shell.py`; Test `tests/tools/test_shell.py`.

**Produces:** `run_shell(workspace, command, timeout_s=30, output_cap=20_000) -> str`. Pipeline: reject metacharacters `;&|<>$(){}` and backticks → tokenize (shlex) → reject `..`/`~`/absolute-path args → first-token allowlist (~30 cmds) → argument guards (`find` `-exec/-execdir/-ok`; `git` `-c/--exec-path`; bare `xargs`/`env` → non-allowlisted program) → deny-pattern list (`rm -rf /`, `sudo`, `curl|sh`, `/etc/passwd`, `chmod 777`, `python -c`, `bash -c`, fork bombs) → `subprocess.run` with **`exec` (list argv, no shell)**, cwd=workspace, timeout, output cap.

Tests: chain bypass `echo ok; cat /etc/passwd` rejected; `find . -exec rm {} +` rejected; `git -c core.pager=...` rejected; `python -c` denied; `sudo` denied; `../x` traversal rejected; absolute path rejected; non-allowlisted first token rejected; allowlisted `ls`/`echo` runs; timeout enforced; output capped.

### Task 7: Tool registry + execute (§4 loop, §7)

**Files:** Create `orchestrator/tools/registry.py`; Test `tests/tools/test_registry.py`.

**Produces:** `tool_defs_for(role) -> list[dict]` (OpenAI tool schema); `tools_for(role) -> set[str]` (researcher: read/list; coder: read/write/list/shell; others: none); `async execute_tool(name, args, workspace) -> ToolResult(content:str, error:str|None, latency_ms)`. Unknown tool → error string.

Tests: researcher denied write/shell defs; coder has all; execute read_file routes; unknown tool returns error.

### Task 8: Roles — prompts, rubrics, temps (§3, §6.1)

**Files:** Create `orchestrator/roles.py`; Test `tests/test_roles.py`.

**Produces:** `ROLE_PROMPTS:dict[role,str]` (~200 tok each); `ROLE_RUBRICS:dict[role,str]` for researcher/coder/synthesizer (§6.1 criteria); `rubric_for(role)`; `system_prompt(role)`; `critic_prompt(role)` = critic base + rubric_for(role). Output-shape instruction (JSON schema) embedded per role.

Tests: every WORKER_ROLE has a prompt; rubric_for returns role criteria; critic_prompt embeds rubric.

### Task 9: Redaction (§9)

**Files:** Create `orchestrator/redaction.py`; Test `tests/test_redaction.py`.

**Produces:** `redact(obj, *, field_allowlist=None) -> obj` deep-walks dict/list/str; scrubs secret patterns (`sk-...`, bearer tokens, `AKIA...`, `KEY=`/`TOKEN=`/`SECRET=` env lines, generic high-entropy `xxx_live_...`) → `"[REDACTED]"`. `redact_preview(text, max_len)` truncates + scrubs.

Tests: API key scrubbed; `.env` line scrubbed; bearer token scrubbed; ordinary text untouched; nested dict walked; preview truncates.

### Task 10: Observers (§9)

**Files:** Create `orchestrator/observers.py`; Test `tests/test_observers.py`.

**Produces:** `Observer` base with one method per event (§9 table: `run_started, plan_ready, subtask_started, llm_call, tool_call, critic_score, subtask_finished, run_finished, flush`); all no-op by default. `safe(observer, event, **fields)` wraps in try/except. `NoopObserver`; `JSONLObserver(stream_or_path)` writes one JSON line/event with previews redacted; `LangfuseObserver` (optional import, redaction default-on, degrades to Noop if package/creds absent). `select_observer(config)` auto-selects: tracing creds → Langfuse else JSONL→stdout/`OBSERVER_LOG_FILE`.

Tests: safe() swallows observer exception; JSONL emits valid line per event; JSONL redacts secret in llm_call preview; auto-select picks JSONL without creds; buggy observer doesn't propagate.

### Task 10b: Budget tracker

**Files:** Create within `orchestrator/orchestrator.py` (or `orchestrator/budget.py`); Test `tests/test_budget.py`.

**Produces:** `Budget(max_llm_calls, max_total_tokens, run_timeout_s, monotonic_start)`; `note_call(usage)`, `exhausted() -> str|None` (returns reason or None), checks calls/tokens/time.

Tests: call ceiling triggers; token ceiling triggers; time ceiling triggers; None token budget never triggers on tokens.

### Task 11: Plan validation + layering (§5.1)

**Files:** Create `orchestrator/plan_validation.py`; Test `tests/test_plan_validation.py`.

**Produces:** `validate_plan(plan) -> list[list[str]]` (topo layers) raising `PlanValidationError(defect_msg)` on: dup id, role not in allowed set, dangling dep, cycle (naming the cycle). `topological_layers(subtasks) -> list[list[str]]`.

Tests: valid plan → correct layers; duplicate id; bad role; dangling dep names the id; cycle names the cycle; diamond DAG layers correctly.

### Task 12: Augmentation (§5.2)

**Files:** Create `orchestrator/augmentation.py`; Test `tests/test_augmentation.py`.

**Produces:** `augment_plan(plan, original_task) -> Plan`: inject coder (deps = researcher ids or all) if none; inject synthesizer (deps = all existing) if none; idempotent; injected deps only reference pre-existing ids (preserves acyclicity).

Tests: missing coder injected + wired; missing synthesizer injected + wired; already-complete plan unchanged (idempotent); post-augmentation plan still validates (acyclic).

### Task 13: Worker loop + dependency data-flow + context budget (§4, §4.1, §4.2)

**Files:** Create `orchestrator/worker.py`; Test `tests/test_worker.py`.

**Produces:**
- `render_user_task(subtask, dep_results:dict[str,WorkerResult], cap=200_000) -> str` — task + "Context from prior steps" block of each dep's `summary`+`artifacts` (+`confidence` when degraded/low); truncate oldest/lowest-confidence first to cap.
- `async run_worker(llm, role, subtask, dep_results, workspace, config, observer, budget) -> WorkerResult` — the §4 loop: build messages, loop max_steps, on no tool_calls parse via `extract_worker_result`, else execute tools + append + `enforce_context_budget`. Emits `subtask_started/llm_call/tool_call`. Notes budget per call.
- `revise(...)` reuses prior messages + critic issues/suggestions.

Tests (FakeLLM): worker that emits final JSON immediately; worker that calls a tool then answers (dep injected); render includes upstream summary+artifacts and low-confidence flag; context budget trims oldest tool result not system/task; max_steps fallback parses last content.

### Task 14: Critic loop, threshold, convergence (§6)

**Files:** Create `orchestrator/critic.py`; Test `tests/test_critic.py`.

**Produces:**
- `is_accepted(score, threshold) -> bool` = `score.approved and score.score >= threshold`.
- `async critic_score(llm, role, worker_result, original_task, config, observer, budget) -> CriticScore` (uses critic_prompt(role); invalid output → `CriticScore.failed_validation()`; emits `critic_score`).
- `async run_critic_loop(llm, role, subtask, worker_result, original_task, revise_fn, config, observer, budget) -> tuple[WorkerResult, CriticScore]` implementing §6 pseudo-code **exactly**: assign `worker_result = revised` unconditionally each scored iteration (v2 fix); convergence guard `no_progress` (field-identical or score not improving) breaks; positional, value-aware equality.

Tests: rejected→revised→accepted returns the **revised** result (regression for v1 discard bug); threshold gates a high score below threshold; critic rejection authoritative; identical revision stops early (convergence); invalid critic output → failed_validation then iterate.

### Task 15: Synthesizer + final critic (§6.3)

**Files:** Create `orchestrator/synthesizer.py`; Test `tests/test_synthesizer.py`.

**Produces:** `async synthesize(llm, approved_results:dict, original_task, config, observer, budget) -> WorkerResult`; `async run_synthesis(...)` runs synth then a final critic pass with synthesizer rubric; if rejected by threshold, iterate synth up to `max_iterations_per_subtask` (same loop as §6). Returns `(final_result, final_score)`.

Tests: synth merges approved results; final critic rejects then synth iterates; accepted final returned; degraded inputs surface low confidence.

### Task 16: Orchestrator.run() end-to-end (§2, §11)

**Files:** Create `orchestrator/orchestrator.py`, `orchestrator/__init__.py`; Test `tests/test_orchestrator.py`.

**Produces:** `class Orchestrator(llm, config)` with `async run(task) -> FinalReport`:
1. emit `run_started`; plan via planner role (validate→retry-with-corrective-prompt up to N→raise); augment; emit `plan_ready`.
2. For each topo layer: `asyncio.gather(*workers, return_exceptions=True)`; exception→degraded result, siblings not cancelled; each result gated by critic loop **before** stored as dep input.
3. budget check between layers → if exhausted, stop new work, synthesize from what exists.
4. synthesize + final critic; build `FinalReport`; emit `subtask_finished`/`run_finished`.
5. `finally`: emit `run_finished` (if not already) + `await observer.flush()`; register `atexit` backstop.

Tests (FakeLLM, scripted plan+workers+critic): happy path migrate-style fixture → FinalReport with approved synthesis; planner emits cycle → corrective retry → success; one worker raises → degraded, run continues; budget exhausted mid-run → graceful synth + run_finished emitted; run_finished always emitted via finally.

### Task 17: End-to-end fixture run + validation sweep (§14)

**Files:** Create `tests/fixtures/` (tiny repo + token v1→v2), `tests/test_e2e_fixture.py`, `orchestrator/__init__.py` exports finalize.

**Produces:** A scripted-FakeLLM end-to-end run over a fixture workspace performing a token v1→v2 "migration" with a coder writing a file and run_shell-"verifying", producing a complete trace tree (JSONL observer captured) mapping run→plan→layers→subtasks→llm→tool→critic→final, with previews redacted. Assert no stderr noise; budgets respected.

Tests: full run emits all expected event types in order; trace redacted; final confidence set; silent stderr.

---

## Self-Review notes

- Spec coverage: §2 exec order→T16; §4/4.1/4.2→T13; §5.1→T11; §5.2→T12; §6/6.1/6.2/6.3/6.4→T14,T15,T8; §7 file→T5, shell→T6, registry→T7, §7.3 honest-threat comments→T6/T7; §8→T2; §9 events/observers/redaction/flush→T9,T10,T16; §10→T3,T10b; §11→T16 (+degraded paths in T2,T13,T14); §13 drivers are emergent; §14 validation→T17 + per-module tests.
- DEGRADED_CONFIDENCE single constant: defined T1, referenced T2/T13/T15.
- Positional indexing + v2 discard fix + convergence: T14.
- Final-answer verification: T15.
