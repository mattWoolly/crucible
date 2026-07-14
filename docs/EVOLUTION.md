# The Evolution of a Small-Model Agentic Orchestrator

**Milestone: first fully-green, model-produced build (`verified=PASS`), 2026-07-08.**

This is the field journal of hardening a minimal agentic orchestrator until a
*small, cheap model* (MiniMax) could autonomously build a real project ("musa",
a local-first music-intelligence app) and have it pass its own `ruff` + `pytest`
gate — verified from a clean checkout, not the model's say-so.

The method was deliberately evolutionary: **run it → observe the failure →
find the root cause with trace evidence → make ONE mutation → run again.** Every
mutation was committed and TDD'd. Each generation fixed the previous bottleneck
and exposed a subtler one. The failure modes got progressively deeper — which
is itself the signal that the harness was maturing.

---

## The setup

- **Engine** (`agent-orchestration-test-minimax-cc`): a generic orchestrator —
  planner → validated DAG → topologically-layered, critic-gated parallel
  workers → synthesizer → final critic. Model-agnostic. Started at 115 tests.
- **Driver** (`musa-cc-orchestrator`): a thin control harness that points the
  engine at the musa brief, in a sandboxed workspace, with a JSONL trace.
- **Target** (`musa`): CLI + library — audio-only music analysis, resumable
  ingest, DSP features, pluggable models, metadata DB + vector index, four
  query modes. Non-trivial: a real ~40-50 file Python project.
- **Models tried**: MiniMax-Text-01 → MiniMax-M2 → MiniMax-M3.
- **Ground truth**: every claim below was checked by actually running
  `ruff` + `pytest` from a clean checkout — never trusting the model or the
  reported confidence.

---

## Phase A — First contact: can it even use tools?

### A1. The hallucinated "done" (model: Text-01)
**Symptom:** First full build reported `confidence 0.90`, 93k tokens — and wrote
**zero files**. The `README.md` present was leftover from an earlier dry-run.

**Evidence:** every `llm_call` had `tool_calls_count: 0`. The model never called
a single tool across the whole run.

**Root cause:** the worker JSON contract said *"Respond with ONE JSON object and
nothing else. No prose outside the JSON."* A literal-minded small model obeyed
on turn one — emitting a final summary that *claimed* work it never did. The
critic (same weak model) rubber-stamped it.

**Mutation** (`ebd576b`): rewrite the contract to phase tool-use — *work in
turns, call tools, and only emit the final JSON once no tool call is pending;
never fabricate results.* Verified in isolation: prompt before → `tool_calls=0`;
after → `tool_calls=1`.

**Worked:** researchers immediately started reading files for real.

### A2. Flailing on phantom paths (model: Text-01)
**Symptom:** 15 tool calls now — but all `list_files`/`read_file`, **zero
writes**, and most **errored**: `list_files("workspace")`, `read_file(
"workspace/Research/...")`, `list_files("/")`.

**Root cause:** the agent had no idea what was in the workspace, so it guessed
paths that didn't exist and burned its budget on errors. Also, a subtle
packaging bug: the driver had installed the engine **non-editably**, so source
fixes weren't even reaching the runtime.

**Mutations:** inject a **workspace orientation** block (real recursive file
listing + the path convention) into every worker's first message
(`b2d93ec`); fix the editable install so source edits propagate.

**Verdict on Text-01:** even with tools wired, it wouldn't do multi-file coding
from abstract tasks. **Retired it.** → MiniMax-M2.

### A3. The planner that refused to plan (model: M2)
**Symptom:** M2 runs died at planning — `PlanValidationError: plan has no
subtasks` after 4 attempts.

**Evidence:** M2 (an *agentic* model) emitted pseudo tool-call syntax
(`<minimax:tool_call>ls -la</...>`) instead of a plan — it wanted to explore
before planning, but the planner has no tools.

**Mutation** (`e7294d6`): tell the planner it has **no tools** and must answer
directly; **ground it** with the same workspace orientation; make the
corrective retry say *"you have no tools — respond with the plan directly"*
instead of the misleading "no subtasks". Verified: valid 13-subtask DAG first
try on the real brief.

---

## Phase B — Trust: making the harness honest and resilient

### B1. The rubber-stamp critic (the pivotal finding)
**Symptom:** M2 built 3,204 LOC of real code — but an increment later *added
tests it never ran* (calling `PathSpec.from_paths`, a hallucinated API),
regressed the suite, and the critic **approved all of it** at high confidence.

**Root cause:** the critic scored work on *vibes*; it never ran the project's
own checks. The definition of done was the model's opinion, not ground truth.

**Mutation** (`f46ced9`): the **verify gate**. An injected verifier runs the
project's real command (`ruff` + `pytest`) after synthesis; on failure it drives
a bounded **repair loop** feeding the actual failure output back to a coder,
then re-verifies. `FinalReport.verified` now carries ground truth; a failed gate
caps confidence and says so. The engine stays sandbox-pure — the *caller*
supplies the runner (dependency injection, like the LLM client).

**Worked, immediately and forever after:** the orchestrator could no longer
report success over broken code. Every subsequent run was honest.

### B2. Resilience: it wasn't quota, it was rate limits
**Symptom:** a run died mid-build with a 429 after retries; the dashboard showed
plenty of quota.

**Root cause:** a full DAG *layer* fires ~7 workers + critics at once, tripping
MiniMax's per-minute burst limit. The generic retry (4 attempts, ~3.5s total)
had no chance.

**Mutations:**
- Rate-limit-aware backoff: 429s get their own budget (6 retries, 10s→60s
  capped, jittered) + a concurrency cap (semaphore) so layers can't burst
  (`7b4a42b`).
- Synthesis `LLMError` now **degrades** to a partial report instead of crashing
  and losing 15 subtasks of work (`0ced4c5`).
- Driver **auto-commits partial work on crash**, and dry-runs use a temp
  workspace so the canned coder can't clobber real files (`914f39b`).

---

## Phase C — The evolutionary series (model: M3)

Switched to MiniMax-M3 (newer, stronger, token-hungry). Ran **A/B pairs** from
an identical seed each generation to see both signal and variance.

### Gen 1 — baseline M3: two failures, both instructive
- **Run 1:** 197/198 tests "passing" — but **not reproducible**. It never
  declared its runtime deps (`numpy`, `librosa`); the "pass" was against a venv
  the build had polluted with ad-hoc installs. From a clean checkout it
  collapsed. **Lesson: a green number can be a lie; verify from clean.**
- **Run 2:** worse (78 pass / 21 fail / 33 err). Trace showed the repair loop
  saw the *same* output all 3 passes and made no progress.

**Root cause of run 2:** the gate ran `ruff check . && pytest`, and **`&&`
short-circuits** — a ruff failure meant pytest never ran, so the repair worker
**never saw the 21 test failures** hiding behind it.

### Gen 2 (evo-a / evo-b) — independent checks
**Mutation** (`ba44472`): run ruff and pytest **independently**, feed *all*
failing output to the repair worker; run tools via `uv run --with` so a missing
dev-dep can't cause spawn errors; bump repair passes.

**Result:** the repair loop now ran its **full 3 passes** (win!) — but both
still `verified=FAIL` (ruff 39-58 errors). Deps declared in only 1 of 2.

**Why it still failed — the killer evidence:** the repair workers were *fighting
the sandbox to verify their own work*. Trace counts, evo-a: `uv` tried **21×,
all blocked**; `ruff` **7×, blocked**; `.venv/bin/pytest` **10×, blocked**; plus
`PATH=`/`PYTHONPATH=` tricks and `tmp_*.py` workaround scripts. **The model knew
it had to verify, tried every way to run the checks, and the sandbox refused all
of them — so it fixed blind.**

### Gen 3 (evo-c / evo-d) — sight
**Mutation** (`7f32549`): add `uv` and `ruff` to the sandbox allowlist (no new
capability beyond the already-allowlisted `python`/`pip`, per the honest §7.3
threat model); point the repair + coder prompts at the working invocation
(`uv run ruff check .` / `uv run pytest -q`, as separate commands); tell them to
declare missing deps.

**Result — big step:**
- Successful self-checks: **0 → 73** (evo-c).
- ruff: 39-58 → **0 (evo-d) and 16 (evo-c)** — the agent can fix what it can see.
- Deps declared: **2 of 2** → reproducibility trap closed.
- evo-c: within **~2 test failures** of green — the closest yet.
- Still `verified=FAIL`: **test failures wouldn't close.**

**Why:** each repair pass was a fresh **cold-start** worker (16 steps) that
re-discovered the whole codebase. Fine for "fix 3 lint lines", hopeless for
"understand 20 interrelated test failures". Three amnesiac attempts, not one
session.

### Gen 4 (evo-e / evo-f) — persistent session
**Mutation** (`8c1ab76`): `continue_worker()` — repair passes 2+ **resume the
prior pass's message history** instead of cold-starting, so the coder keeps
everything it learned (like a human debugging). Bump passes to 5.

**Result:** self-checks up (93, evo-e); heavy, sustained work; deps declared;
ruff low (11-15). **But still `verified=FAIL`** — tests stuck at ~few / 20.
Across ~8 from-scratch M3 runs, **none ever reached green.** The hypothesis
hardened: maybe this is M3's capability ceiling on last-mile debugging.

### Gen 5 (repair-e1) — the breakthrough: split build from repair
**The decisive, cheap experiment.** Instead of another from-scratch pair, take
the closest near-miss (evo-e: 15 ruff + a few tests) and run a **repair-only
brief that continues from the existing build** (do not rebuild), with 10 repair
passes.

**Result: `verified=PASS` ✅** — first time ever. Trajectory: `FAIL → FAIL →
FAIL → PASS` (green on the 3rd repair pass, 7M tokens). Confirmed from a clean
checkout: `ruff` clean, `pytest` green.

**What it proved:** **it was never a capability ceiling — it was a phase
problem.** From-scratch runs spent their budget *building* and hit the wall
before repair could finish. The model could always close the gap; it just needed
the **build phase and the repair phase separated.** This is the single biggest
reliability lever of the entire project — bigger than any one gate or prompt fix.

### Gen 6 (gen6-a) — the two-phase recipe, baked in
**Mutation** (driver `acce247`): automate gen-5's winning recipe. `--auto-repair`
turns "produce a green build" into one command — after a `verified=FAIL` build,
`_auto_repair_loop` continues the **repair** brief against the same committed
workspace until green or `--max-repair-runs`. Each phase stays a **separate
`Orchestrator.run()` with its own budget** — the build/repair separation is
preserved, not merged. Hermetically TDD'd (`tests/test_auto_repair.py`, 6 tests:
stop-on-green, cap, multi-repair-then-green, and the three no-op cases).

**Live run (M3, 600-call budget, `--auto-repair --max-repair-runs 3`):** the
build finished `verified=PASS` (81 files, 22.5M tokens) — so the **outer**
auto-repair correctly **did not fire** (the loop skips a build that's already
green). Confirmed from a **clean checkout**: fresh `uv sync`, `ruff` clean,
**147 tests pass**, deps declared → reproducible.

**But read the trace before calling this a gen-6 win — it isn't, quite.** The
build did **not** land green one-shot; its **inner** verify-repair loop (gen-3/4
machinery, `max_verify_repairs=10`) ground through **9 verify passes** to get
there — `FAIL×3 (ruff RUF046) → ruff clears, pytest still red ×2 → ruff
re-breaks as new files land (I001, B007, RUF002) → PASS on pass 9/10`, right at
the edge of the inner budget. So what actually closed the gap was the **larger
600-call budget letting the pre-existing inner loop finish**, not anything new
in gen-6. The gen-6 mutation (the *outer* auto-repair) was armed and idle.

**What this run does and doesn't show.** *Does:* the two-phase pipeline runs
end-to-end, bare seed → green, in one hands-off command with the safety net
armed (an operational win over gen-5's manual two-step). *Doesn't:* any
reliability/quality improvement attributable to gen-6 itself — the terminal
metric (green from clean checkout) is identical to gen-5, and the outer loop
never fired. It also usefully bounds the gen-5 lesson: with a generous enough
build budget the inner loop alone can converge a from-scratch build; the outer
build/repair split matters most when the build budget is too tight for the inner
loop to finish (exactly the starvation gen-5 diagnosed). **To observe a gen-6
win you need a `verified=FAIL` build where the outer auto-repair drives it
green** — one high-variance run away, since gen6-a's build converged internally.

---

## Phase D — Cross-model portability: a second model reaches green (z.ai GLM-5.2)

Everything above was MiniMax. The question Phase D answers: **is the harness
model-agnostic in practice, or MiniMax-tuned by accident?** Added a `PROVIDERS`
registry in the driver (one dict entry per OpenAI-compatible backend; provider
inferred from the model name) and pointed it at **z.ai's GLM Coding Plan**
(`glm-5.2`, endpoint `api.z.ai/api/coding/paas/v4`). A one-call `smoke_model.py`
preflight confirms endpoint+auth+model for cents before a real build.

**Result: GLM-5.2 reached `verified=PASS`, green from a clean checkout** (37
files, ruff clean, 82 tests; tag `green-glm-5.2`). Trajectory: a from-scratch
build landed `verified=FAIL` (38 files, confidence 0.10), then a **repair-
continuation** (continue-don't-rebuild, the gen-5 recipe) closed it to green —
verify `FAIL → PASS` in 2 passes, ~98 llm_calls / 2M tokens. This is the first
model *outside the MiniMax family* to drive the harness end-to-end, and it
cleared the same planner hurdle M2 failed (valid DAG first try).

**Two findings the run surfaced:**

1. **The stalled-provider wedge (fixed).** GLM-5.2's *first* attempt hung 16+ min
   in the repair phase — blocked in `ep_poll` on an open socket to z.ai, having
   already blown past the 2h `run_timeout_s`. Root cause: `OpenAIClient` built
   `AsyncOpenAI` with **no request timeout**, and the run budget is only checked
   *between* operations, so a single hung network read is never interrupted; the
   SDK's hidden internal retries stacked timeout cycles silently. **Fix** (engine
   `69cf3f2`): a per-request timeout (300s) + `max_retries=0` so our own logged
   backoff is the single retry authority — a stall now raises a retryable
   `APITimeoutError` and recovers, or surfaces a bounded `LLMError` instead of
   hanging forever. Provider-agnostic; MiniMax simply never stalled this way.
   The retry after the fix got past the exact call that hung in ~57s.

2. **GLM-5.2 is slow and shell-chatty on this harness.** Its from-scratch build
   took **~2.5h** (vs M3's ~40 min for a comparable one), partly thrashing on
   **shell-metacharacter refusals** — it favors compound `;`/`>`/`{}` commands
   the sandbox rejects, burning tool calls re-trying. Untested cheap mutation:
   a shell-style hint in the coder prompt ("run commands separately; no
   `;`/`&&`/redirects"). The *repair* phase, by contrast, was fast and cheap.

**What Phase D proves:** the reliability levers found on MiniMax — ground-truth
gates, give-the-agent-sight, separate-build-from-repair, continue-don't-rebuild
— **transfer to a different model family unchanged**. The harness is the
product, not the model.

### Phase D2 (glm-b) — validating the harness improvements live, and the first auto-repair firing

Re-ran GLM-5.2 from a fresh seed with four new mutations in place (per-request
timeout `69cf3f2`, quote-aware shell guard `805bb05`, tail-kept verify trace
`d31639b`, ad-hoc-install guard `9c145a5`) and `--auto-repair` on. Results:

- **The outer auto-repair loop fired and reached green — the first time.** Build
  `verified=FAIL` (55 files, a `DecodeError`); `_auto_repair_loop` ran repair-1
  against the same workspace → **`verified=PASS`** on the first repair run.
  Confirmed from a clean checkout: ruff clean, **244 passed, 1 skipped**; tag
  `green-glm-5.2-b`. This is the live *build→outer-repair→green* trajectory
  gen6-a never produced (its build converged internally).
- **Shell friction ~halved:** refusals **7% → 3%** (20/501 vs 28/399). The
  quote-aware guard + coder note did what the trace evidence predicted.
- **No ad-hoc installs; deps declared** → reproducible green (the guard held).
- **A bug the run surfaced (fixed `b4cab3e`/`8cade00`):** the repair phase's
  trace was empty — `run()` ends with `observer.flush()`, and the JSONL
  observer *closed* the file on flush, so gen-6's second `run()` (sharing one
  observer) wrote to a closed stream. flush() now persists without closing;
  close() is a separate end-of-life call the driver makes once. Fittingly, the
  first firing of the gen-6 loop is what exposed a latent gen-6 observability
  bug — evidence-driven to the end.

---

## The numbers

| Run | Model | Gen | Files | Self-checks | ruff | pytest | Tokens | verified |
|-----|-------|-----|-------|-------------|------|--------|--------|----------|
| first build | Text-01 | A1 | 0 | 0 | — | — | 93k | fake 0.90 |
| test-01 | M2 | A/B | ~31 | 0 | many | many fail | 3.0M | FAIL |
| M3 run1 | M3 | 1 | 48 | few | — | 197/1* | 15.6M | FAIL |
| M3 run2 | M3 | 1 | 33 | few | 39 | 78/21+33e | 12.0M | FAIL |
| evo-a | M3 | 2 | ~48 | 15 | 58 | 15 fail | 9.6M | FAIL |
| evo-b | M3 | 2 | ~35 | 32 | 39 | pass | 9.3M | FAIL |
| evo-c | M3 | 3 | 51 | **73** | 16 | ~2 fail | 13.6M | FAIL |
| evo-d | M3 | 3 | 37 | 10 | **0** | 29 fail | 2.9M | FAIL |
| evo-e | M3 | 4 | ~50 | **93** | 15 | few | 12.5M | FAIL |
| evo-f | M3 | 4 | ~55 | 58 | 11 | 20 fail | 20.1M | FAIL |
| **repair-e1** | **M3** | **5** | (evo-e) | 36 | **0** | **green** | **7.0M** | **PASS ✅** |
| **gen6-a** | **M3** | **6** | 81 | — | **0** | **147 pass** | **22.5M** | **PASS ✅** |
| glm build | GLM-5.2 | D | 38 | — | — | fail | ~2.5h | FAIL |
| **glm repair** | **GLM-5.2** | **D** | 37 | — | **0** | **82 pass** | **2.0M** | **PASS ✅** |
| glm-b build | GLM-5.2 | D2 | 55 | — | — | fail (DecodeError) | 9.9M | FAIL |
| **glm-b auto-repair** | **GLM-5.2** | **D2** | 55 | — | **0** | **244 pass** | **3.1M** | **PASS ✅** |

\* not reproducible — polluted venv. gen6-a: green within a single build run —
its **inner** verify-repair loop converged at pass 9/10; the **outer**
auto-repair never fired. Verified from a clean checkout. Same terminal outcome
as gen-5 (both PASS); gen-6's gain is operational (one-command pipeline), not a
metric jump.

---

## What worked

- **Ground-truth gates over model judgment.** The verify gate was the turning
  point: the moment success meant "the real checks pass," every other
  improvement had an honest scoreboard to move.
- **Trace-driven root-causing.** Every mutation came from *evidence in the
  trace* (tool_call counts, blocked-command tallies), never a guess. The "uv
  blocked 21×" finding was invisible until we counted.
- **Give the agent sight.** The biggest per-generation jump was letting the
  coder run the project's own checks. A blind agent cannot converge.
- **Persistent context for debugging.** Threading repair passes into one session
  matched how debugging actually works.
- **Separate build from repair.** The breakthrough. Don't ask one budgeted run
  to both create and perfect; build, then repair-continue to green.
- **Continue, don't rebuild.** Rebuilding from scratch re-rolls the variance
  dice and re-pays the build-budget tax. Iterate on a good state.
- **Injected dependencies (verifier, LLM, sleep).** Kept the engine pure and
  every mutation unit-testable with fakes — no live-LLM tests, fast TDD.

## What didn't work / dead ends

- **Trusting a passing test count.** Non-reproducible green (polluted venv) fooled
  us once. Clean-checkout verification is non-negotiable.
- **Chasing a stronger model as the fix.** M3 over M2 helped quality but did
  **not**, by itself, reach green — the harness/workflow was the real lever.
- **From-scratch A/B pairs to force convergence.** Expensive (~20-30M
  tokens/pair) and high-variance; a single cheap, targeted repair-continuation
  answered the question that four generations of full builds couldn't.
- **Blunt guards.** An early idea — "reject any coder that writes no files" —
  was rightly abandoned; some coder subtasks legitimately only verify.
- **`&&` in the gate command.** Convenient, but short-circuiting blinded the
  repair loop to everything after the first failing check.

## Cross-cutting lessons for small-model agents

1. Small models take instructions *literally* — "respond with only JSON" means
   they skip the work. Phase the turns explicitly.
2. Orient before acting — give the agent the workspace state; don't make it
   guess paths.
3. The sandbox that protects you can blind the agent. Let it run the project's
   own read-only checks.
4. Honesty first: a system that reports failure truthfully is worth more than
   one that reports success optimistically.
5. Variance is real; reliability comes from the *feedback loop*, not from
   eliminating the model's randomness.

---

## Milestone state (2026-07-08)

- **Engine:** 154 tests green; 14 substantive reliability mutations since the
  115-test baseline (see `git log`: `ebd576b` → `dffae94`, incl. the per-request
  timeout, a quote-aware shell guard, tail-kept verify output, a source-level
  guard against ad-hoc installs, the flush-doesn't-close fix, and a selectable
  batch/incremental repair strategy). Driver adds `--verify-isolated`
  (clean-env reproducibility gate) and `--repair-strategy`.
- **Driver:** verify gate, multi-provider registry (MiniMax + z.ai GLM),
  gen-6 auto-repair, rate-limit resilience, crash-safe commits, one-call
  provider preflight; 21 hermetic tests.
- **musa (green, model-built):** `~/projects/musa-evo-e` — `verified=PASS`,
  reproducible from clean checkout (commit `ea67965`).
- **musa (green, gen-6 one-command build):** `~/projects/musa-gen6-a` — tag
  `green-gen6`, green in the build phase alone, verified from clean checkout.
- **musa (green, GLM-5.2 / z.ai):** `~/projects/musa-glm-a` — tag
  `green-glm-5.2`, build→repair-continuation to green, verified from clean
  checkout. First non-MiniMax model to reach green.
- **musa (green, GLM-5.2, one-command auto-repair):** `~/projects/musa-glm-b` —
  tag `green-glm-5.2-b`, build→**outer auto-repair**→green (244 tests), verified
  from clean checkout. First live firing of the gen-6 loop.
- **Also green (hand-finished M3 build):** `~/projects/musa-test-m3`.

## Open threads / next evolutions

- **Bake in the two-phase workflow — ✅ done (gen-6).** `--auto-repair` /
  `--max-repair-runs` in `run_musa.py` (`_auto_repair_loop`, hermetically
  TDD'd). Live-validated: gen6-a reached `verified=PASS`, green from a clean
  checkout (`green-gen6` tag). One follow-up worth capturing: a live run where
  the build *fails* and auto-repair actually fires to green (gen6-a's build
  passed directly, so the firing path was only exercised hermetically).
- **Observability — ✅ done (`d31639b`).** The verify trace now keeps a 4000-char
  *tail* (not a 1000-char head), so pytest's summary line survives and the
  per-pass failure trajectory is readable. Event-aware redaction; every other
  preview unchanged.
- **Shell-sandbox friction — ✅ partly done (`805bb05`).** GLM-5.2 refused 7% of
  build tool_calls on shell metacharacters. Made the metachar guard *quote-aware*
  (operators outside quotes still blocked; quoted literals like
  `git commit -m "x (v1)"` now allowed) and added a concrete coder shell note.
  Validated against the 28 real refusals AND live: glm-b's refusal rate fell
  **7% → 3%**, confirming the prompt note + guard together cut the thrashing.
- **Stalled-provider timeout — ✅ done (`69cf3f2`, Phase D).** Per-request
  timeout so a hung provider can't wedge a run.
- **Reproducibility trap — ✅ closed at the source (`9c145a5`).** The gen-1
  non-reproducible-green happened because a coder installed deps ad-hoc into the
  venv without declaring them. The shell tool now denies `pip install` /
  `uv pip install` and steers to `uv add` (which records the dep) — so an
  undeclared dep can't be installed, hence must be declared to pass. Traces
  confirmed coders really reach for ad-hoc installs. Complements, not replaces,
  the clean-checkout discipline.
- **Inline code per §7.3 (candidate).** 9 of GLM's 28 refusals were
  `uv run python -c "import x; print(version)"` dep-checks. Since write_file +
  `python file.py` already gives code-exec (SPEC §7.3 is explicit the sandbox is
  a speed bump), *allowing* inline code would cut that friction with no real
  posture change — but it reverses a deliberate guard, so it's a decision, not a
  mechanical fix. Left conservative for now.
- **Auto-repair firing live — ✅ done (Phase D2, glm-b).** GLM-5.2 build failed,
  the outer `_auto_repair_loop` fired and reached `verified=PASS` on repair-1
  (green from clean checkout, tag `green-glm-5.2-b`). The gen-6 loop is now
  proven end-to-end, not just hermetically.
- **Reproducibility gate (runtime clean-env verify) — ✅ done (`931056a`).**
  `--verify-isolated` runs the gate with `UV_PROJECT_ENVIRONMENT` at a fresh
  dir, so `uv run` syncs ONLY declared deps — a polluted `.venv` can't make it
  pass. Validated live against real `uv`: green `musa-glm-b` → PASS; the same
  build with librosa undeclared → FAIL (`ModuleNotFoundError`). Belt-and-
  suspenders with the source-level install guard (prevention + detection).
- **Repair strategy — apparatus ✅ done (`dffae94`); A/B ran, inconclusive by
  design.** Ran batch vs incremental as GLM-5.2 repair-continuations from an
  identical fixed failed state (evo-f, 20 test failures). BOTH reached green,
  reproducible from clean checkout (100 / 75 tests) — but BOTH passed verify at
  `attempt=0`: the repair brief's SUBTASKS fixed everything before the
  verify-repair loop ran, so `--repair-strategy` (which governs only that loop)
  was never exercised in either arm. The 2× token gap (6.1M vs 3.0M) is build
  variance, not strategy. **Finding: the strategy is a *build-flow* lever (the
  inner loop that fires when synthesis emits failing code, e.g. gen6-a's 9
  passes), not a repair-continuation lever — repair-continuation subtasks
  pre-empt the loop.** A real strategy A/B would need two full builds (batch vs
  incremental auto-repair), which reintroduces build variance and costs ~2
  builds — not worth it for a lever with such a narrow practical surface. The
  run's real result: GLM-5.2 repaired a 20-failure M3 build to green twice from
  an identical start (cross-model repair robustness).

## 2026-07-10 — Full-screen TUI (Textual): Crucible gets a face

**Why.** Verdict from use: without a real terminal UI the orchestrator "won't
be used much." Requirement: Claude Code-type functionality — full-terminal
app, splash logo, slash commands (permissions / theme / model), live token
utilization.

**What.** New `orchestrator/tui/` package (Textual), `crucible` console
script (`uv run crucible -w ./proj`). The TUI is a pure *client* of the
engine's public seams:

- **Events in** — `TUIObserver` funnels the standard Observer events onto the
  Textual message bus; a live plan tree (layers → subtasks with status glyphs
  + critic confidence), an event transcript, and a status bar with
  token/call/tool utilization all render from the same events the JSONL trace
  sees. `/tokens` gives per-role spend.
- **Control out** — ONE new engine seam: `Config.tool_gate`, an async
  `(tool, args) -> bool | (bool, reason)` consulted in the worker loop before
  every tool call (`None` = headless behavior unchanged; a raising gate
  degrades to denial per §11 — a broken UI can't kill a run). The TUI's
  `PermissionGate` implements `auto`/`ask`/`readonly` modes with a
  session allowlist and a serialized modal prompt (parallel workers can't
  stack modals; an `always` answer un-blocks the queue behind it).
- Slash commands (`/help /permissions /theme /model /tokens /plan /workspace
  /threshold /clear /quit` + aliases), shell-style input history, `esc`
  cancels the run task (engine `finally` still emits `run_finished`+flush).

**Tests.** 154 → 202, all hermetic: parser, gate semantics (incl. the
concurrent-ask race), stats (injected clock), engine `tool_gate` hook
(FakeLLMClient; denial blocks the side effect AND returns an ERROR tool
message), and headless Textual pilot tests for the app shell. Screenshots
verified via `save_screenshot` in `run_test` — no live terminal needed.

**Gotcha caught by tests.** `CrucibleApp._running()` silently collided with
Textual's own `App._running` bool → `TypeError` at dispatch; renamed
`_run_active`. Also: the repo's `.venv` still pointed at the pre-rename
project path (`agent-orchestration-test-minimax-cc`) — stale shebangs made
`uv run pytest` fall back to a user-level pytest; `rm -rf .venv && uv sync`
fixed it.

**Open.** Token ceiling in the status bar reads the default config (no
per-run `max_total_tokens` yet); per-role model routing would slot into
`/model` naturally; streaming token counts need a richer `llm_call` event.

## 2026-07-10 — Provider registry in the engine + native Anthropic backend

**Why.** The TUI read only `LLM_API_KEY`/`LLM_BASE_URL`, so a fresh shell
"forgot" the keys the musa driver knew about. Requirement: pick up
`GLM_API_KEY` / `MINIMAX_API_KEY` / `ANTHROPIC_API_KEY` (+ `OPENAI_API_KEY`
for later) automatically.

**What.**
- `orchestrator/providers.py` — the driver's provider knowledge, promoted into
  the engine: glm (z.ai coding endpoint, `GLM_API_KEY`/`ZAI_API_KEY`/
  `Z_AI_API_KEY`, default `glm-5.2`), minimax (`MiniMax-M3`), anthropic
  (`claude-opus-4-8`), openai (key but no default model — must be named).
  Resolution: explicit `LLM_API_KEY` escape hatch wins; else first available
  in `glm → minimax → anthropic → openai`. `make_client()` picks the client
  by provider kind.
- `orchestrator/anthropic_client.py` — a second real `LLMClient`, on the
  **official `anthropic` SDK** (per Anthropic's guidance: native Messages
  API, never the OpenAI-compat shim). Translates the engine's OpenAI shapes:
  system msg → `system` param; assistant `tool_calls` → `tool_use` blocks;
  consecutive `role:"tool"` results → tool_result blocks merged into ONE user
  turn (Anthropic requires parallel results in a single message); function
  schemas → `input_schema`. Deliberate omissions, both API-mandated on
  current Claude models: **temperature is not forwarded** (`temperature`/
  `top_p`/`top_k` → 400 on Opus 4.7+/Sonnet 5/Fable 5 — the engine's per-role
  temperature is a no-op there) and **no `thinking` param** (adaptive
  thinking would require replaying thinking blocks verbatim across turns,
  which the OpenAI-shaped message store can't carry — open thread below).
  Same retry architecture as OpenAIClient: SDK retries disabled, engine
  backoff loop is the single authority, dedicated long 429 budget. A safety
  `refusal` comes back as HTTP 200 + empty content → worker degrades (§11),
  no crash.
- TUI: banner/header show provider:model; `/model` lists providers with ✓/✗
  key status; `/model <provider> [model]` switches; `/model <name>` sets the
  model on the current provider. anthropic is allowed to launch with no env
  key (the SDK resolves `ant auth login` profiles). `run.py` uses the same
  resolution.

**Tests.** 202 → 227, all hermetic: registry resolution (env-isolated
fixture), translation unit tests, injected fake SDK for the client (verifies
temperature is NOT sent, tool_use parsing, usage mapping, retry/rate-limit
budgets, refusal path), TUI provider switching. **Live-validated** both ways:
`resolve_from_env()` picked glm/glm-5.2 from the real shell, and one minimal
real call to `claude-opus-4-8` round-tripped a tool call
(`ping {value: 7}`, 451 tokens) through the schema translation.

**Open.** Adaptive thinking for the Anthropic backend needs thinking-block
replay (stash native content blocks alongside the OpenAI-shaped history);
worth doing if Claude becomes a primary worker model rather than an option.
The engine's role temperatures silently don't apply on Claude — acceptable
while critics/planner steering comes from prompts, but worth a note in SPEC
if the backend graduates.

## 2026-07-10 — The Operator: a conversational representative in front of the swarm

**Why.** Crucible was batch (brief in → verified report out); the
conversational half of the loop — clarify intent, compose the brief, fire the
run, interpret the trace, decide to repair — was always done manually by
Claude Code. The user asked for that role in the product: "an orchestrator
agent that can act as my representative."

**What.** `orchestrator/operator.py` — an agent whose tool IS the
orchestrator. Deliberately hand-rolled on the engine's own machinery (LLMClient
protocol + a persistent-session loop mirroring §4), NOT an external agent
framework: the rep must run on any provider (GLM/MiniMax/Claude), stay
hermetically testable with FakeLLMClient, and respect the DI thesis.

- One persistent chat session; per message the operator answers directly or
  composes a **self-contained brief** and calls `run_swarm` (prompt encodes
  the brief-writing lessons from the musa runs: swarm has no conversation
  memory; state done-criteria incl. verification; describe current-vs-desired
  for repairs; don't prescribe the DAG). `read_file`/`list_files` for
  grounding.
- **The verify gate stays sacred**: the tool result leads with
  `verify gate: PASS/FAIL`; the prompt forbids claiming success beyond
  `verified=True`. The rep interprets, it never vouches.
- Routed through the same PermissionGate: in `ask` mode, dispatching the swarm
  itself pops the confirmation modal *with the brief shown* — a free
  plan-approval step. Swarm failures return as tool ERROR strings (§11),
  never crash the chat.
- Session hygiene: a turn cancelled mid-tool-call leaves dangling
  `tool_calls`; `_heal_dangling()` backfills cancellation results so the
  session stays replayable. Step budget bounds the loop.
- TUI: plain input now chats with the operator (`you` / `◆ rep` lines);
  `/swarm <task>` is direct batch dispatch; `/reset` fresh session; `/rep`
  shows/sets the operator's model — **defaulting to the big brain (anthropic
  key → claude-opus-4-8) while the swarm stays on `/model`'s small model**.
  First real use of per-role model routing, at the rep/swarm boundary.

**Tests.** 227 → 243, hermetic: conversation persistence, dispatch + report
feedback, gate denial, swarm-failure containment, empty-brief rejection,
grounding tools, dangling-tool-call healing, step budget; TUI routing tests
with a stub operator. **Live-validated**: operator on real claude-opus-4-8
read a scratch workspace via its tools (2 tool calls) and answered correctly
WITHOUT dispatching the swarm when told not to.

**Open.** No operator-level token budget (per-chat Budget like the engine's);
the rep's system prompt will want tuning after the first real build driven
through conversation; a `/plan-confirm`-style edit-the-brief-before-dispatch
affordance falls out of the ask-mode modal but could be richer (edit, not
just allow/deny).

## 2026-07-10 — Splash glitch (and the alignment bug it monetizes)

The logo looked staggered because Textual's `text-align: center` centers each
line independently and the ANSI-shadow lines had ragged widths. Fix +
feature in one: `logo_lines()` pads to uniform width (true alignment), and the
splash now *glitches in* — per-line horizontal jitter with decaying amplitude
at 20fps for ~1.2s, occasional corrupted-scanline color flickers, then a snap
to the aligned gradient. Key-dismiss works mid-glitch. `logo_markup()` grew
`offsets`/`colors` params (width-preserving shifts), unit-tested; a pilot test
drives `_glitch_tick()` past the window deterministically and asserts the
settled frame is uniform-width. 243 → 251 tests.

## 2026-07-11 — WI-34: native OpenTelemetry telemetry (ported from Determa)

**Why.** Per-provider/per-model usage (tokens, latency), run/subtask/tool
spans, and verify outcomes should land in the existing self-hosted
Prometheus + Loki + Grafana stack alongside Claude Code / Codex telemetry.
Spec: `WI-34-otel-telemetry.md` (Determa's WI, structure reused; names and
seams translated to Crucible).

**What.** `orchestrator/otel.py` — an `OTelObserver` riding the Observer
seam (the Crucible-native equivalent of Determa's `CoreSessionDeps.telemetry`
injection; zero engine API changes), chained behind the UI/JSONL observer via
a new `MultiObserver` (safe fan-out: one broken sink can't starve the rest).
Load-bearing choices mirrored from the Determa implementation: providers are
LOCAL (never OTel globals); span parenting is EXPLICIT keyed by `subtask_id`
(parallel workers interleave events — ambient context would mis-parent);
instrument names in `.` form; high-cardinality ids are span attributes only;
in-memory reader/exporter injectable for hermetic tests; disabled default
returns None (zero OTel code runs). Retro-spans reconstruct start times from
event latency since engine events arrive post-completion. Config:
`CRUCIBLE_OTEL=1`, `CRUCIBLE_OTEL_ENDPOINT` (default gRPC :4317, same path
as Claude Code), `CRUCIBLE_OTEL_ENV`, `CRUCIBLE_OTEL_TRACES=0`.

**Engine accounting fixes riding along:** `llm_call` events now carry
`usage_prompt`/`usage_completion` (token type split for the `type` label),
and the critic finally emits `llm_call` at all — its tokens previously hit
only the Budget, undercounting the TUI stats and any observer.

**Metrics** (Prometheus names): `crucible_token_usage_total{provider,model,
role,type}`, `crucible_llm_duration_milliseconds_*`, `crucible_run_count_total`,
`crucible_tool_count_total{tool,ok}` + duration, `crucible_verify_count_total
{passed}`, `crucible_critic_score_*{approved}`, `crucible_subtask_duration_
milliseconds_*{role}`. **Traces:** `crucible.run` → `crucible.subtask` →
`crucible.llm`/`crucible.tool`.

**Tests.** 251 → 265, hermetic (InMemoryMetricReader/InMemorySpanExporter —
no network): token fan-out by type incl. zero-skip and total-fallback, all
counters, span hierarchy + retroactive timing, crashed-run span closure,
no-high-card-labels assertion, env config, MultiObserver isolation.

**Live-validated** against the real stack: synthetic events through real
OTLP/gRPC → all 8 instruments on the collector `:8889` with correct
translated names → Prometheus query `sum by (model,type)
(crucible_token_usage_total)` returns the glm-5.2 input/output split.

**Findings for the monitoring stack (not Crucible):**
1. The collector's traces pipeline 404s: Loki has no `/otlp/v1/traces`
   endpoint (logs-only OTLP). Crucible's spans reach the collector and are
   then dropped — pre-existing for ANY trace producer in this stack. Fix is
   collector-side: add a trace store (Tempo) or point traces elsewhere.
2. Grafana `ai-agents` dashboard extension (spec §7) is the remaining ops
   step; PromQL is ready in README/this entry.

**Open.** No cost metric yet — Crucible has no per-model pricing table
(roadmap: token/cost accounting); when it grows one, `crucible.cost.usage`
drops in beside the token counter. Operator chat llm_calls are exported when
the TUI's OTel observer is enabled (they ride the same observer).

## 2026-07-11 — Persistent telemetry config (`~/.config/crucible/config.json`)

WI-34 §5's config-file layer, completing env-only enablement:
`TelemetrySettings.load()` resolves env > config file > defaults (env can
force-disable what the file enables). File shape
`{"otel": {"enabled": true, "endpoint": ..., "environment": ...,
"traces": ...}}`; path overridable via `CRUCIBLE_CONFIG`; a missing or
corrupt file silently falls back — telemetry config must never break
startup. A suite-wide conftest fixture points `CRUCIBLE_CONFIG` at a
nonexistent path so tests can never read the developer's real config (which
now enables OTel and would otherwise spin real exporters inside unit tests).
265 → 268 tests. The user's machine now has telemetry on persistently.

## 2026-07-12 — Read roots: agents may look outside the workspace (read-only)

**Why.** Tasks often reference a sibling project ("port the pattern from
`../determa`") but the sandbox hard-rejected `..`, absolute paths, and
symlink escapes — agents were blind outside the workspace.

**What.** `Config.read_roots: tuple[str, ...]` — extra directories agents may
READ. Asymmetric on purpose: `read_file`/`list_files` gain a
`resolve_readable()` path (workspace OR any root, reachable via `..`/absolute
paths, same realpath-first symlink discipline — a symlink inside a root
pointing somewhere un-rooted is still rejected, and root-name prefixes don't
leak `sibling` → `sibling-2`); `write_file` keeps the strict
`resolve_in_workspace()` rule and `run_shell`'s path guards are untouched —
**mutation never leaves the workspace regardless of roots**. Default empty:
containment behavior byte-identical.

Plumbing: `execute_tool(..., read_roots)`; `workspace_orientation()` names
the roots so models know reads may go there (and states writes still can't);
root listings render absolute paths (workspace-relative naming would be
ambiguous). Operator shares ONE mutable roots list with the TUI so
`/roots add ../x` applies mid-conversation without a session reset. Surfaces:
TUI `/roots [add|rm|clear]`, `run.py --read-root` (repeatable).

**Tests.** 268 → 281: default-unchanged, `..`/absolute into roots allowed,
outside-all-roots denied, prefix-leak and symlink-escape rejections, writes
never widened (files + registry level), orientation conditional, worker
end-to-end via Config, operator live-mutation of the shared list, /roots
command. Pre-existing suite untouched — proves default behavior identical.

## 2026-07-13 — Highlight / copy / paste in the TUI

Textual 8 already ships the hard parts — click-drag text selection is on by
default for RichLog (`ALLOW_SELECT`), `ctrl+c`/`super+c` copies the selection
via the Screen binding, and clipboard writes go over OSC 52 (mirrored on
`app.clipboard` for tests). What Crucible was actually missing:

- **Multi-line paste silently truncated** — Textual's Input keeps only line 1
  of a bracketed paste. `PromptInput._on_paste` now flattens newlines to
  spaces so a pasted paragraph/brief lands whole (headless-verified before:
  `"line one"`; after: full text).
- **`/copy [reply|report|all]`** — mouse-free grabs of the things you
  actually want out of a session: the last operator reply (default), the last
  final-report summary (tracked from BOTH dispatch paths: /swarm's panel and
  the operator's run_swarm), or the whole transcript.

281 → 284 tests: paste flattening, /copy sources + clipboard mirror + empty/
usage paths, selection opt-in assertions. Terminal note: OSC 52 requires
terminal support (WezTerm yes); shift+drag remains the terminal-native
fallback and is documented in the README.

## 2026-07-13 — Transcript rewrite: RichLog → Transcript (selection actually works)

User report: click-drag did nothing in the output window. Diagnosis via
headless mouse simulation: the drag REGISTERED (a Selection appeared in
`screen.selections`) but start/end never resolved — `Widget.get_selection`
extracts text only from widgets whose `_render()` yields Text/Content, and
RichLog renders pre-baked strips → always None. A Textual limitation, not a
config problem; `allow_select=True` was a red herring.

Fix: new `Transcript(VerticalScroll)` widget — one Static per write(), same
write()/clear() surface, parallel plain-text log (`plain_lines()`) for /copy
all and tests, MAX_ENTRIES cap (oldest lines drop), stick-to-bottom
auto-scroll unless the user scrolled up. Statics render Text/Content so
selection resolves for real (pilot-verified: drag extracts the exact
substring).

Bug found by the rewrite: Rich SILENTLY ignores unknown span styles at print
time but Textual's renderer RAISES — so markup like `[auto|ask|readonly]` in
usage strings (parses as a tag, fails as a style) went from harmless in
RichLog to app-crashing in Static. `_validated_markup()` now Style.parse()s
every span eagerly and degrades to literal text; the handful of UI strings
with intentional bracket-literals now escape() them properly.

284 → 286 tests: drag-selection resolves text (the regression test that
would have caught this), renderables + bad markup don't crash, entry cap.
