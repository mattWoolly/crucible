# Design Spec: Minimal Agentic Orchestrator (v2)

A spec for a small-model agentic harness that completes complex tasks by spawning specialist sub-agents. The quality bar is "Opus-level" but every worker runs on a small, cheap model.

This document describes **what** the system is and **why** it's shaped this way. It is intentionally implementation-free — another agent reading this spec should be able to build an equivalent system in any language, in any LLM framework, without seeing the existing code. Where a mechanism is load-bearing (e.g. how dependency outputs reach a downstream worker), the spec is explicit enough to build two compatible implementations.

> **Changes from v1.** This revision fixes two control-flow bugs (accepted-revision discard, synthesize/critique ordering), specifies the previously-undefined dependency data-flow, adds DAG validation, recalibrates the security claims (the allowlist is a speed bump, not a boundary), adds global budget ceilings, and adds trace redaction. Changed sections are marked **(v2)**.

---

## 1. Purpose

Given a complex task (e.g. "migrate this codebase from token format v1 to v2 and verify with tests"), produce a high-quality, verified result by:

- Decomposing the task into a small DAG of subtasks
- Running each subtask on a specialist prompt (researcher, coder, etc.)
- Passing upstream results into the workers that depend on them
- Iterating on weak results via an independent critic
- Merging everything into a final answer, then verifying the merge

The orchestrator itself runs the same model as the workers. Quality comes from the structure (decomposition, isolation, critique, verification), not from a smarter model.

## 2. Architecture

```
                    task
                     │
                     ▼
              ┌─────────────┐
              │   Planner   │  → JSON plan of subtasks (DAG)
              └──────┬──────┘
                     │
                     ▼
            ┌────────────────────┐
            │ Plan Validation    │  ← schema + DAG checks; see §5.1
            │ + Augmentation     │  ← defensive layer;     see §5.2
            └────────┬───────────┘
                     │ topologically layered subtasks
        ┌────────────┼────────────┐
        ▼            ▼            ▼
   researcher     coder      researcher   ← run in parallel within a layer
        │            │            │           dependency outputs injected
        │            │            │           into dependents (§4.1)
        ├──── critic loop per worker (§6) ────┤  ← gate each result before it
        │            │            │              becomes a dependency input
        └────────────┼────────────┘
                     ▼
              ┌─────────────┐
              │ Synthesizer │  ← merges APPROVED worker outputs
              └──────┬──────┘
                     ▼
              ┌─────────────┐
              │ Final Critic│  ← verifies the MERGED answer (§6.3)
              └──────┬──────┘
                     │ accepted by threshold? else iterate synth (max N)
                     ▼
              ┌─────────────┐
              │ FinalReport │  → summary, worker results, critic scores, confidence
              └─────────────┘
```

Every step is observed through a pluggable Observer interface. Every LLM call, tool call, and critic score emits an event.

**Execution order (authoritative — resolves the v1 diagram/text contradiction):**

1. Plan → validate → augment.
2. For each topological layer, run its workers in parallel.
3. **Each worker result is gated by the critic loop (§6) before it is allowed to flow to its dependents or to the synthesizer.**
4. The synthesizer runs once over approved results.
5. A final critic pass gates the synthesized answer; if rejected, the synthesizer iterates.

## 3. Roles

Each role is a system prompt + tool set. The execution loop is identical across roles; only the prompt and tools differ. This is a deliberate simplification: one loop to test, one loop to instrument.

| Role | Purpose | Tools | Constraints |
| --- | --- | --- | --- |
| `planner` | Decompose task into subtask DAG | none | Output must be valid JSON matching the plan schema (§5.1). Acyclic; no dangling deps. |
| `researcher` | Gather information by reading files | read_file, list_files | No edits. Cite paths. |
| `coder` | Edit files and run commands | read_file, write_file, list_files, run_shell | Verify with run_shell when possible (tests, linters). |
| `critic` | Score a worker or merged result | none | Output a score 0–10, a boolean `approved`, and a structured rubric (§6.1). Be specific. |
| `synthesizer` | Merge worker outputs into final answer | none | Do not introduce new work. State plainly if anything is unresolved. |

Each role prompt is **short** (~200 tokens). The model is small; long system prompts are noise. The critic prompt additionally carries a **per-role rubric** so it scores against explicit criteria rather than vibes (§6.1).

## 4. The execution loop (shared by all roles)

For every role except planner/critic/synthesizer (which have no tools):

```
messages = [system_prompt, render_user_task(subtask, dependency_inputs)]   # ← §4.1
for step in 0..max_steps:
    response = LLM(messages, tools=tool_definitions, temperature=role_temp)  # ← §10
    if no tool_calls in response:
        return parse_json(response.content)   # ← the role's "answer"
    for tool_call in response.tool_calls:
        result = execute_tool(tool_call, workspace)
        append tool_result to messages
    enforce_context_budget(messages)          # ← §4.2
return parse_json(last_assistant_content)     # ← fallback if max_steps hit
```

**The loop is identical across roles** because the only thing that changes is the system prompt, which tools are registered, and the (low) temperature. This makes the code small (one loop) and the behavior predictable.

### 4.1 Dependency data-flow (v2 — previously unspecified)

The DAG is only meaningful if a worker can see the outputs of the subtasks it `depends_on`. This is the mechanism:

- Each subtask declares `depends_on: [id, ...]` and optional `inputs` (free-text describing what it needs from upstream — a hint for the synthesizer/critic, not a routing key).
- When a worker starts, the orchestrator renders its initial user message as: **the subtask's `task`**, followed by a **"Context from prior steps"** block containing, for each upstream dependency, that dependency's **`summary` + `artifacts`** (not its full transcript).
- Injected context is subject to the same size cap as a file read (~200KB total across all dependencies; oldest/lowest-confidence truncated first). This keeps isolation (a worker never sees unrelated branches) while making the DAG functional.
- If an upstream dependency produced a **degraded or low-confidence** result, its `confidence` is included in the injected block so the downstream worker (and later the critic) can react to it rather than trusting it blindly.

This preserves the §13.2 isolation property: a worker sees its own task plus *only* its direct ancestors' distilled outputs — never the global window.

### 4.2 Per-worker context budget (v2)

Tool results accumulate in `messages`. A worker that calls `read_file` twenty times can blow its own context. After each tool round, enforce a context budget (token or byte ceiling): drop or summarize the oldest tool results first, never the system prompt or the original task. This is a knob (§10).

## 5. Plan validation and augmentation (defensive layer)

The planner is a small model. Its output must be both **structurally valid** and **complete enough to succeed**. Two passes, in order.

### 5.1 Validation (v2 — new)

Before augmentation, reject/repair plans that cannot execute:

1. **Schema.** Each subtask has `{id, role, task, depends_on, inputs}`. `id` unique. `role` in the allowed set.
2. **No dangling dependencies.** Every id in any `depends_on` exists in the plan.
3. **Acyclic.** The dependency graph must be a DAG. Run a topological sort; if it fails, the graph has a cycle.
4. **Layerable.** Topological layers are computed here and reused by the executor (§2 step 2).

On validation failure, retry the planner with a corrective system prompt naming the specific defect ("subtask `c3` depends on `c9` which does not exist" / "cycle: c1 → c2 → c1"). If still failing after N retries, raise (§11).

### 5.2 Augmentation

After the plan is valid, ensure it is complete:

1. If no subtask has role `coder`, append one:
   - depends on any `researcher` subtasks (or all existing subtasks if none)
   - task = the original user task, framed as "complete the original task using prior worker outputs"
2. If no subtask has role `synthesizer`, append one:
   - depends on all existing subtasks
   - task = "merge all worker outputs into a final answer"

Augmentation must be **idempotent**: a plan that already has coder + synthesizer is unchanged. Augmentation runs *after* validation, so injected subtasks must themselves preserve acyclicity (they only ever depend on pre-existing ids).

This is the single most important reliability feature. Without it, the orchestrator's success rate tracks the planner's competence. With it, the orchestrator always produces a complete result, and a bad planner's output is silently repaired.

## 6. The critic loop

The critic gates **each worker result before it becomes a dependency input or reaches the synthesizer** (§2). This ordering matters: a weak researcher output that's never gated would poison every downstream coder. Per worker result:

```
score = critic(worker_result, original_task, rubric_for(role))
score = orchestrator_enforce_threshold(score)       # override approved flag
i = 0
while not is_accepted(score, threshold) and i < max_iterations_per_subtask:
    revised = worker.revise(worker_result, score.issues, score.suggestions)  # keeps prior context
    revised_score = orchestrator_enforce_threshold(critic(revised, original_task, rubric_for(role)))
    worker_result = revised            # ← v2 FIX: always keep the revision we just scored
    score = revised_score
    if no_progress(revised_score, score) : break    # ← §6.2 convergence guard
    i += 1
# worker_result now holds the last (and best-available) revision, accepted or not
```

**v2 fix — accepted revisions are no longer discarded.** In v1 the loop did `if accepted: break` *before* assigning `worker_result = revised`, so a revision that finally passed was thrown away and the stale rejected version was returned. Here `worker_result = revised` happens unconditionally for every revision actually scored, so the result that the critic approved is the result that propagates.

### 6.1 Per-role rubric (v2)

Because the critic runs the *same small model* as the worker, role separation alone leaves correlated blind spots. Give the critic an explicit, structured rubric per role so it scores against criteria, not vibes:

- `researcher` → are claims cited to real paths? gaps acknowledged? no edits attempted?
- `coder` → does it build/test? were changes verified via run_shell? any unhandled cases?
- `synthesizer` → are all worker outputs accounted for? are unresolved items stated? does stated confidence match content?

The critic returns the rubric line-items alongside the scalar score, and these surface in the `critic_score` event.

### 6.2 Convergence guard (v2)

If a revision is field-identical to its predecessor, or the score fails to improve, stop early rather than burning the full retry budget on a stuck worker. (Equality is positional/value-aware per the indexing note below.)

### 6.3 Final-answer verification (v2 — new)

The synthesized answer — the thing the user actually receives — gets its own critic pass with the `synthesizer` rubric. If rejected by threshold, the synthesizer iterates (same loop, same `max_iterations`). In v1 only worker outputs were gated; the merge step shipped unverified.

### 6.4 Threshold enforcement

The critic prompt tells the model to set `approved=true` when score ≥ some hardcoded value. The orchestrator treats the threshold as a real, configurable knob:

```python
def is_accepted(score, threshold):
    return score.approved and score.score >= threshold
```

This means:
- A critic rejection is authoritative (threshold cannot rescue it).
- A high critic score can still be rejected if below threshold.
- A lower threshold makes the orchestrator more lenient; a higher one stricter.
- Thresholds may be set **per role** (§10): a coder's output is test-verifiable and can hold a higher bar than open-ended research.

**Iteration index** must use positional indexing, not value-equality (e.g. `list.index(item)`). Two workers can produce field-identical results; positional updates must hit the right slot.

## 7. Tool sandbox

Workers have access to file and shell tools. The sandbox is layered. **Read §7.3 first: the sandbox is a speed bump, not a security boundary. The boundary is the container.**

### File tools (`read_file`, `write_file`, `list_files`)

- All paths resolved via symlink- and `..`-aware resolution.
- Containment check: resolved path must be inside the workspace.
- Directory traversal rejected (workspace escape).
- File size cap on reads (~200KB) to avoid context blowup.

### Shell tool (`run_shell`)

The shell tool is the highest-risk surface. The constraints, in order of importance:

1. **Run without an intervening shell.** Use `exec` with tokenized argv, not `sh -c`. Metacharacters (`;`, `&&`, `||`, `|`, `$()`, backticks, `>`, `<`) become inert — literal characters inside argv, not shell syntax.
2. **Reject at the source.** Any command containing shell metacharacters (`;&|<>$(){}` backticks) is refused before tokenization. Defense in depth on top of (1).
3. **Block path traversal.** Arguments containing `..` components, `~` prefixes (home expansion), or absolute paths are refused. The model must use relative paths.
4. **Allowlist first-token.** The first token of argv must be in an allowlist of ~30 commands (python, pytest, git, ls, cat, grep, find, etc.).
5. **Argument-level guards on exec-capable allowlisted commands (v2).** Several allowlisted commands are arbitrary-code vectors *by design* and survive every layer above because they need no metacharacters:
   - `find … -exec / -execdir {} \;` runs arbitrary binaries.
   - `git -c <anything>=…`, git hooks, aliases, and pagers execute arbitrary code.
   - `xargs`, `env`, and similar re-launch arbitrary programs.

   For these commands, reject the dangerous flags (`-exec`, `-execdir`, `-ok` for find; `-c`, `--exec-path`, `-c protocol.ext.allow` for git; bare `xargs`/`env` invoking non-allowlisted programs). Without this, `find . -exec rm {} +` passes the v1 sandbox cleanly.
6. **Deny-pattern list** for specific known-bad commands (`rm -rf /`, `sudo`, fork bombs, `curl|sh`, `/etc/passwd`, `chmod 777`, `python -c`, `bash -c`, etc.).
7. **Timeout** (30s) and output cap (~20KB) to prevent runaway commands.

The combination of (1) and (2) closes the chain-bypass class (e.g. `echo ok; cat /etc/passwd`). The combination of (4) and (5) is needed to close the exec-via-allowlisted-command class.

### 7.3 Honest threat model (v2 — recalibrated)

The allowlist is **not** a real security boundary, and the spec should not claim it is:

- A `coder` has `write_file` + an allowlisted `python`/`pytest`. It can write a `.py` file and execute it. `python -c` being denied is cosmetic — `python wrote_this.py` is full code execution.
- Even with §7.5 argument guards, the allowlisted interpreters are Turing-complete escape hatches.

Therefore: the sandbox's job is to stop *accidental* and *low-effort* escapes and to make logs legible — **not** to contain an adversarial model. **For any untrusted task, the orchestrator MUST run inside a container or restricted-execution environment (gVisor, microVM, seccomp-confined namespace, etc.).** The in-process sandbox is defense-in-depth layered *inside* that boundary, never a substitute for it.

## 8. JSON output handling

Every role except planner outputs JSON. Small models frequently produce JSON that is *almost* valid — wrong escape, trailing text, multiple objects, markdown code fences. The extraction layer handles these gracefully:

1. Strip markdown code fences if present.
2. Try direct `json.loads`.
3. Try `json.JSONDecoder().raw_decode` scanning balanced `{…}` candidates; prefer the **largest balanced object** (or the last one) rather than blindly the first — small models reason in prose then emit, and the first `{` is often an inline example, not the answer. (v2 refinement of v1's "first `{`".)
4. Try again with invalid-escape sanitization (replace `\<not-a-valid-escape-char>` with `\\<char>`).
5. **If all parsing fails, return a degraded result, do not throw:**
   ```json
   {"summary": "<first 500 chars of raw text>",
    "artifacts": {},
    "confidence": DEGRADED_CONFIDENCE,
    "uncertainties": ["parse_failed: <reason>"]}
   ```

`DEGRADED_CONFIDENCE` is a single named constant (default 0.1), referenced everywhere a degraded result is constructed and everywhere the synthesizer routes on it — no magic numbers duplicated across §8 and §11 (v2).

The orchestrator must keep running on degraded results. The critic will score them low; the synthesizer will see the low confidence signal.

## 9. Observability

Every event in the orchestrator emits through a pluggable Observer interface. The interface is **push-based** (one method per event), not pull-based, so multiple observers can be chained and observers can be swapped without changing the orchestrator.

**Events:**

| Event | When | Fields |
| --- | --- | --- |
| `run_started` | Beginning of `run()` | task, workspace |
| `plan_ready` | After validation + augmentation | reasoning, subtask_count, layers |
| `subtask_started` | Before each worker runs | subtask_id, role, task, dependency_ids |
| `llm_call` | After every LLM round-trip | role, model, messages preview, output preview, latency_ms, usage (tokens), tool_calls_count |
| `tool_call` | After every tool invocation | subtask_id, name, args, result preview, latency_ms, error |
| `critic_score` | After every critic call | subtask_id, score, approved, issues, rubric |
| `subtask_finished` | After each worker completes | subtask_id, role, summary, confidence |
| `run_finished` | End of `run()` (always, even on failure) | summary, confidence, iterations, tokens_total |
| `flush` | Before observer is discarded | (none) |

**Three observers ship:**

1. `NoopObserver` — silent default.
2. `JSONLObserver` — one JSON line per event to stdout or a file. Trivially useful for debugging and CI.
3. `LangfuseObserver` (or any tracing backend) — full trace tree in a web UI.

**Auto-select** based on environment variables: if tracing backend credentials are present, use that observer; otherwise fall back to JSONL to stdout.

**Redaction (v2 — new).** `llm_call` and `tool_call` previews carry file contents, tool args, and tool output straight into a third-party trace UI. Before any preview leaves the process, run a redaction pass: a configurable field/pattern allowlist plus secret-pattern scrubbing (API keys, tokens, `.env` values). Default-on for the tracing observer. A file-reading agent must not exfiltrate the repo into the trace backend.

**Robustness.** The observer must not crash the orchestrator. All observer calls are wrapped in try/except so a buggy observer never aborts a run.

**Cleanup (v2 — corrected emphasis).** The orchestrator's own `finally` block performs the authoritative flush: `await observer.flush()` / `observer.shutdown()` while the event loop is still alive. An `atexit` handler is registered only as a *backstop* for hard-exit paths. Doing the real flush in `finally` (not `atexit`) is what avoids async-cleanup-on-a-dead-loop noise; suppressing OTel/library shutdown stderr is then a secondary cosmetic patch, not the primary mechanism.

## 10. Configuration

### Environment

| Var | Purpose |
| --- | --- |
| `LLM_API_KEY` | API key for the model gateway |
| `LLM_BASE_URL` | OpenAI-compatible endpoint |
| `LLM_MODEL` | Model name (default to the orchestrator's model) |
| `OBSERVER_TRACING_PUBLIC_KEY` | Tracing backend public key (optional) |
| `OBSERVER_TRACING_SECRET_KEY` | Tracing backend secret key (optional) |
| `OBSERVER_TRACING_HOST` | Tracing backend host (optional) |
| `OBSERVER_LOG_FILE` | If set, JSONL events go to this file instead of stdout |

### Knobs (constructor args)

| Knob | Default | Effect |
| --- | --- | --- |
| `workspace` | (required) | The directory the agents operate in. All file paths are sandboxed to it. |
| `approval_threshold` | 8 | Minimum critic score (0-10) for a result to be accepted. Critic's `approved` is a hint; the gate is `approved and score >= threshold`. A critic rejection is authoritative. |
| `approval_threshold_by_role` | `{}` | Optional per-role override of `approval_threshold` (e.g. stricter for `coder`). |
| `max_iterations_per_subtask` | 1 | Retry budget when the critic rejects (applies to workers and to the final synthesis pass). |
| `max_steps` | 8 | Per-worker tool-use loop bound (§4). |
| `worker_context_budget` | ~120K tokens | Per-worker accumulated-message ceiling before oldest tool results are trimmed (§4.2). |
| `max_llm_calls` | 200 | Global ceiling across the whole run. Raises on exhaustion. |
| `max_total_tokens` | (provider-dependent) | Global token budget across the whole run. |
| `run_timeout_s` | 600 | Wall-clock ceiling for the entire run. |
| `role_temperature` | `{planner:0.0, critic:0.0, default:0.2}` | Low temp for planner/critic improves determinism and reproducible traces. |
| `observer` | None | Pass a custom observer; otherwise auto-select based on env. |

## 11. Error handling and degraded modes

The orchestrator must never crash on recoverable errors. Degraded modes:

| Failure | Behavior |
| --- | --- |
| LLM call fails (network, 5xx, parse) | Retry up to N times with backoff inside the LLM client. If still failing, raise — the run cannot continue. |
| Worker produces invalid JSON | Parse with the layered extractor (§8). If all layers fail, return degraded result. The worker still "completes" with `DEGRADED_CONFIDENCE`. |
| Worker produces degraded result | Critic scores it. Iteration kicks in. If still degraded after max iterations, it stays degraded and the synthesizer gets a low-confidence input (flagged via §4.1). |
| One worker in a parallel layer raises | The layer is gathered with `return_exceptions=True` (v2): the failed worker becomes a degraded result; siblings are **not** cancelled. The run continues. |
| Plan validation fails (schema/cycle/dangling) | Retry planner with a corrective system prompt naming the defect (§5.1). If still failing, raise. |
| Critic produces invalid output | Default to score 5, approved False, issues=["critic output failed validation"]. The worker iterates. |
| Tool execution fails | Return the error string to the worker. The worker decides what to do. |
| Global budget exhausted (`max_llm_calls` / `max_total_tokens` / `run_timeout_s`) | Stop spawning new work; synthesize from whatever approved/degraded results exist; mark final confidence accordingly. Emit `run_finished`. (v2) |
| Run interrupted (Ctrl-C, timeout) | `run_finished` is still emitted via `finally`; `observer.flush()` runs there. The trace tree is complete. |

## 12. Out of scope (do not build)

Deliberately not in the harness. Each would be a meaningful project on its own.

- **Cross-run memory.** Each `run()` is independent. No history of past runs.
- **Live integration tests.** Unit tests cover everything except the LLM round-trip.
- **CI / packaging / entry-point CLI.** The harness is a library. `python -m orchestrator` is not built.
- **Multi-repo tasks.** The sandbox is one workspace.
- **Streaming UI.** Progress is observable via the observer, not a streaming terminal UI.
- **Container/sandbox isolation as a *deliverable*.** The harness *requires* a container for untrusted tasks (§7.3) but does not provision one — that's the operator's responsibility.
- **Custom role prompts at runtime.** Roles are fixed in code.

## 13. Quality drivers (in order of leverage)

1. **Tight role prompts.** A small model with a clear, narrow role prompt beats a vague "helpful agent." ~200 tokens per role.
2. **Isolated context per worker.** Each worker sees only its own task plus its direct ancestors' distilled outputs (§4.1) — never the global window. No context pollution, no cross-contamination.
3. **Critic in the loop, gating before propagation.** An independent verifier with a per-role rubric (§6.1) catches what self-critique misses, and gates a result *before* it can poison downstream workers.
4. **Plan validation + augmentation.** Reject cycles/dangling deps, then always ensure coder + synthesizer exist. The single most reliable feature.
5. **Verify the final answer, not just the parts (§6.3).** The merge step is gated too.
6. **Structured JSON outputs.** The orchestrator routes on confidence and structure, not vibes. Every output is a schema-validated model.
7. **Parallel where independent, fault-isolated.** Independent subtasks within a layer run concurrently with `return_exceptions=True`. Wall time ≈ slowest path; one failure degrades rather than aborts.
8. **Robust JSON extraction.** Multi-layer extractor + single-source-of-truth degraded fallback.
9. **No-shell execution + argument guards.** Tokenized argv closes chain-bypass; argument-level guards on `find`/`git`/`xargs` close exec-via-allowlist. Both sit *inside* a container (§7.3).
10. **Bounded cost.** Global call/token/time ceilings (§10) keep a cheap-model fan-out cheap.

## 14. Validation

A correct implementation of this spec should:

1. **Pass unit tests covering:**
   - schema validation; **DAG validation (cycle detection, dangling-dep rejection, topological layering)**;
   - JSON extraction (code fences, multi-object → largest/last, invalid escapes, degraded fallback, single `DEGRADED_CONFIDENCE` constant);
   - tool sandboxing (file containment, shell allowlist, deny patterns, chain bypass, path traversal, home expansion, absolute paths, metacharacter rejection, **`find -exec` / `git -c` / `xargs` argument guards**);
   - observer interface **and redaction of secrets in previews**;
   - plan augmentation (idempotency, missing-coder injection, missing-synthesizer injection, dependency wiring, post-augmentation acyclicity);
   - **dependency data-flow (upstream `summary`+`artifacts` injected into dependents; low-confidence flag propagated)**;
   - threshold enforcement (critic rejection authoritative; threshold gates on score; per-role override);
   - **critic loop keeps the accepted revision (regression test for the v1 discard bug)**;
   - **convergence guard (identical/no-progress revision stops early)**;
   - **final-answer critic pass**.
2. **Run end-to-end on a fixture task** (e.g. "migrate fixture_repo from token format v1 to v2 defined in specs/v2.md and verify with pytest") producing a working, tested result.
3. **Emit a complete trace tree** mapping cleanly to the structure (run → plan → layers → subtasks → LLM generations → tool calls → critic spans → final critic), with previews redacted.
4. **Stay silent on stderr** during normal operation, including when async tasks are cancelled mid-run — achieved primarily by flushing in `finally` (§9), not by stderr suppression.
5. **Respect global budgets:** a run that would exceed `max_llm_calls` / `max_total_tokens` / `run_timeout_s` degrades gracefully and still emits `run_finished`.
