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

- **Engine:** 144 tests green; 11 substantive reliability mutations since the
  115-test baseline (see `git log`: `ebd576b` → `d31639b`, incl. the per-request
  timeout that stops a stalled provider wedging a run, a quote-aware shell guard,
  and tail-kept verify output in the trace).
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
  Validated against the 28 real refusals: the 3 legit ones now pass, 25 operator/
  inline-code cases stay blocked. The prompt note (the lever for those 25) is
  unproven until a live GLM run.
- **Stalled-provider timeout — ✅ done (`69cf3f2`, Phase D).** Per-request
  timeout so a hung provider can't wedge a run.
- **Inline code per §7.3 (candidate).** 9 of GLM's 28 refusals were
  `uv run python -c "import x; print(version)"` dep-checks. Since write_file +
  `python file.py` already gives code-exec (SPEC §7.3 is explicit the sandbox is
  a speed bump), *allowing* inline code would cut that friction with no real
  posture change — but it reverses a deliberate guard, so it's a decision, not a
  mechanical fix. Left conservative for now.
- **Repair strategy:** "fix one failure, re-run, repeat" vs. batch — untested.
- **Auto-repair firing live:** still want a run where the build fails and the
  *outer* auto-repair loop drives it green (gen6-a converged internally).
