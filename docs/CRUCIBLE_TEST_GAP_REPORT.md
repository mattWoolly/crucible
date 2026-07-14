# CRUCIBLE — Test Gap Report (vs SPEC §14)

> Scope: unit/integration test coverage of the spec's validation checklist
> (SPEC §14). Built from direct reads of every test file in this session;
> every cited test function was confirmed present in the file named. Per the
> task, this report compiles **only verified findings**. Where a researcher
> could not confirm a sub-item, it is marked **`not yet confirmed`** rather
> than fabricated.

**Checklist note.** SPEC §14 is itself a 5-item list whose first bullet
enumerates ~10 sub-areas, then four numbered top-level items (e2e fixture,
trace tree, silent stderr, budgets). To make the review tractable the work
was split into 37 numbered line-items (1–8 DAG/JSON, 9–18 sandbox,
19–21 + 28–33 critic loop / observer / synthesis, 22–27 augmentation +
dependency data-flow, 34–37 e2e / OTel / budgets). The §14 sub-bullets map
onto those line-items; the mapping is shown in Part B.

---

## PART A — TEST INVENTORY

Suite-wide hermeticity is provided by a **single** autouse fixture in
`tests/conftest.py`: `_isolate_crucible_config(monkeypatch, tmp_path)`,
which points `CRUCIBLE_CONFIG` at a nonexistent file and unsets every
`CRUCIBLE_OTEL*` env var so tests never read the developer's real config or
spin up real OTLP exporters. There is **no** shared `FakeLLMClient` / clock /
verifier fixture in conftest — those are defined inline per module. The LLM
is hermetic throughout: every test uses `FakeLLMClient` (scripted, no
network); OTel tests inject `InMemoryMetricReader` + `InMemorySpanExporter`.
**No live network calls occur in any test.**

| File | Lines | Hermetic / network | Fixtures (inline unless noted) |
| --- | ---:| --- | --- |
| tests/conftest.py | 18 | — | `_isolate_crucible_config` (autouse, suite-wide) |
| tests/test_plan_validation.py | 73 | hermetic (pure model fns) | helper `_p` |
| tests/test_json_extract.py | 58 | hermetic (pure fns) | none |
| tests/test_augmentation.py | 57 | hermetic (pure fns) | helper `_roles` |
| tests/test_worker.py | 138 | hermetic (FakeLLMClient) | none (uses `tmp_path`) |
| tests/test_critic.py | 106 | hermetic (FakeLLMClient) | helpers `_wo`, `_critic_json`, `_revise_fn_from` |
| tests/test_synthesizer.py | 56 | hermetic (FakeLLMClient) | helpers `_critic_json`, module consts `SYNTH_SUB`/`APPROVED` |
| tests/test_models.py | 50 | hermetic (pure models) | none |
| tests/test_budget.py | 32 | hermetic (injected clock) | local `t` dict clock |
| tests/test_observers.py | 124 | hermetic (StringIO / tmp_path) | none; `_Boom(Observer)` |
| tests/test_redaction.py | 42 | hermetic (pure fns) | none |
| tests/test_otel.py | 261 | hermetic (in-memory readers/exporters) | `harness()` (metric reader + span exporter) |
| tests/test_orchestrator.py | 408 | hermetic (FakeLLMClient) | helper fns `_plan_json`/`_wr`/`_approve`/`_config`; classes `RecordingObserver`, `_SynthCrashLLM`, `_FakeVerifier`, `_CountingLLM` |
| tests/test_e2e_fixture.py | 123 | hermetic (FakeLLMClient, capfd) | `workspace(tmp_path)`; helpers `_count_tool_msgs`, `_script` |
| tests/test_tool_gate.py | 97 | hermetic (FakeLLMClient) | helpers `coder_llm`, `sub` |
| tests/test_llm.py | 288 | hermetic (fake chat/OpenAI) | inline fake classes |
| tests/test_config.py | 41 | hermetic (monkeypatch env) | none |
| tests/test_providers.py | 109 | hermetic (monkeypatch env) | autouse `_reset` |
| tests/test_roles.py | 29 | hermetic (pure prompts) | none |
| tests/test_anthropic_client.py | 207 | hermetic (FakeSDK) | helpers `_block`, `FakeSDK` |
| tests/test_operator.py | 196 | hermetic (FakeLLMClient) | none (uses tmp_path) |
| tests/tools/test_files.py | 69 | hermetic (tmp_path, symlinks) | `ws(tmp_path)` |
| tests/tools/test_shell.py | 201 | hermetic (tmp_path) | `ws(tmp_path)` |
| tests/tools/test_registry.py | 35 | hermetic (tmp_path) | none |
| tests/tools/test_read_roots.py | 157 | hermetic (tmp_path tree) | `setup_tree(tmp_path)` |
| tests/tui/*.py | 706 | hermetic (no network) | various — outside §14 scope |

Total ~2700 lines across the core (non-tui) test modules.

---

## PART B — COVERAGE vs SPEC §14 (37 line-items)

Each item is tagged **COVERED** / **PARTIAL** / **THIN** / **MISSING** (or
**`not yet confirmed`**) and cites the real test function(s) that were read.
Researcher-derived citations that this writer re-read and confirmed are
marked with the source; items a researcher could not pin down are explicitly
flagged.

### Cluster 1 — DAG validation + JSON extraction (§14.1 bullets 1–2; items 1–8)
*(R2 cluster — all 8 verdicts re-confirmed by direct reads this session.)*

| # | Item | Verdict | Cited test(s) |
|---|------|---------|---------------|
| 1 | DAG cycle detection | **COVERED** | `test_cycle_detected`, `test_self_dependency_rejected` (tests/test_plan_validation.py) |
| 2 | Dangling-dep rejection (names the defect) | **COVERED** | `test_dangling_dep_names_id` (tests/test_plan_validation.py) |
| 3 | Topological layering | **COVERED** | `test_valid_plan_layers`, `test_diamond_dag_layers` (tests/test_plan_validation.py) |
| 4 | JSON code-fence stripping | **COVERED** | `test_code_fenced` (tests/test_json_extract.py) |
| 5 | Multi-object → largest/last | **COVERED** | `test_prose_then_real_object_prefers_largest`, `test_trailing_text_after_object` (tests/test_json_extract.py) |
| 6 | Invalid-escape sanitization | **COVERED** | `test_invalid_escape_sanitized` (tests/test_json_extract.py) |
| 7 | Degraded fallback, no throw | **COVERED** | `test_extract_worker_result_degraded_on_garbage`, `test_extract_worker_result_schema_mismatch_degrades`, `test_total_garbage_returns_none`, `test_extract_worker_result_happy` (tests/test_json_extract.py) |
| 8 | Single `DEGRADED_CONFIDENCE` constant (no duplicated 0.1) | **COVERED** | `test_extract_worker_result_degraded_on_garbage`, `test_extract_worker_result_schema_mismatch_degrades` (tests/test_json_extract.py); also `test_degraded_uses_named_constant` (tests/test_models.py) |

*(Bonus, schema area: `test_duplicate_id_rejected`, `test_bad_role_rejected`,
`test_empty_plan_rejected`, `test_plain_object` also present — §5.1 schema.)*

### Cluster 2 — Tool sandbox (§14.1 bullet 3, §7; items 9–18)
*(R3 cluster — all verdicts re-confirmed by direct reads this session.)*

| # | Item | Verdict | Cited test(s) |
|---|------|---------|---------------|
| 9 | File containment (symlink/`..`-aware) | **COVERED** | `test_dotdot_escape_rejected`, `test_absolute_path_escape_rejected`, `test_symlink_escape_rejected`, `test_write_escape_rejected`, `test_read_within_workspace` (tests/tools/test_files.py); `test_paths_outside_all_roots_still_rejected`, `test_sibling_name_prefix_does_not_leak`, `test_symlink_out_of_root_rejected`, `test_writes_never_use_read_roots` (tests/tools/test_read_roots.py) |
| 10 | Shell allowlist (first-token; uv/ruff allowed) | **COVERED** | `test_non_allowlisted_first_token`, `test_allowlisted_ls_runs`, `test_allowlisted_echo_runs`, `test_uv_and_ruff_are_allowlisted` (tests/tools/test_shell.py) |
| 11 | Deny-pattern list | **COVERED** | `test_sudo_denied`, `test_chain_bypass_to_sensitive_file_also_rejected`, `test_python_dash_c_denied`, `test_pipe_rejected` (tests/tools/test_shell.py) |
| 12 | Metacharacter / chain-bypass rejection (incl. quote-aware) | **COVERED** | `test_chain_bypass_rejected`, `test_unquoted_chain_still_rejected`, `test_unquoted_redirect_still_rejected`, `test_uv_run_still_blocks_metachars`, `test_quoted_metachars_allowed`, `test_quoted_parens_pass_the_metachar_guard`, `test_quoted_semicolon_is_literal_not_a_chain` (tests/tools/test_shell.py) |
| 13 | Path traversal (`..`) | **COVERED** | `test_traversal_arg_rejected` (tests/tools/test_shell.py); `test_dotdot_escape_rejected` (tests/tools/test_files.py); `test_default_behavior_unchanged_dotdot_rejected` (tests/tools/test_read_roots.py) |
| 14 | Home expansion (`~`) | **COVERED** | `test_home_expansion_rejected` (tests/tools/test_shell.py) |
| 15 | Absolute paths rejected | **COVERED** | `test_absolute_path_arg_rejected` (tests/tools/test_shell.py); `test_absolute_path_escape_rejected` (tests/tools/test_files.py) |
| 16 | `find -exec`/`-execdir`/`-ok` argument guard | **COVERED** | `test_find_exec_rejected`, `test_find_exec_without_metachars_rejected` (tests/tools/test_shell.py) |
| 17 | `git -c` argument guard | **COVERED** | `test_git_dash_c_rejected` (tests/tools/test_shell.py). *Sub-vectors* (`--exec-path`, hooks, aliases, pagers) **not individually tested** — primary `-c` vector covered. |
| 18 | `xargs`/`env` argument guard | **PARTIAL** | `test_xargs_non_allowlisted_program_rejected` (xargs, tests/tools/test_shell.py). **`env` exec-vector case: no test found** — see THIN priority below. |

### Cluster 3 — Critic loop / observer (§14.1 bullets 4, 7–10; items 19–21)
*(R4 cluster — LOW CONFIDENCE 0.10 / sparse artifacts. This writer re-read
`tests/test_critic.py`, `tests/test_observers.py`, `tests/test_redaction.py`
and **confirmed** the functions below; unconfirmed sub-items flagged.)*

| # | Item | Verdict | Cited test(s) |
|---|------|---------|---------------|
| 19 | Observer interface + robustness (buggy observer never aborts run) | **COVERED** | `test_safe_swallows_observer_exception`, `test_base_observer_implements_every_declared_event`, `test_safe_handles_none_observer`, `test_jsonl_emits_one_line_per_event`, `test_flush_does_not_close_so_multi_run_traces_survive`, `test_close_is_idempotent`, `test_auto_select_defaults_to_jsonl`, `test_auto_select_respects_explicit` (tests/test_observers.py) |
| 20 | Redaction of secrets in previews | **COVERED** | `test_jsonl_redacts_secret_in_preview` (tests/test_observers.py); `test_openai_key_scrubbed`, `test_env_line_scrubbed`, `test_bearer_token_scrubbed`, `test_jwt_scrubbed`, `test_nested_dict_walked`, `test_field_allowlist_drops_unlisted`, `test_preview_truncates_and_scrubs` (tests/test_redaction.py) |
| 21 | Threshold enforcement (rejection authoritative; threshold gates score; per-role override) | **COVERED** | `test_is_accepted_requires_both`, `test_enforce_threshold_demotes_high_score_below_threshold`, `test_enforce_threshold_keeps_rejection`, `test_threshold_gates_high_score`, `test_critic_rejection_authoritative` (tests/test_critic.py); `test_per_role_threshold_override` (tests/test_config.py) |

### Cluster 4 — Critic loop internals / synthesis (§14.1 bullets 8–10; items 28–33)
*(R4 cluster — sparse. Re-read by this writer; `tests/test_critic.py` and
`tests/test_synthesizer.py` confirmed.)*

| # | Item | Verdict | Cited test(s) |
|---|------|---------|---------------|
| 28 | Critic keeps the accepted revision (v1 discard-bug regression) | **COVERED** | `test_accepted_revision_is_kept_regression` (tests/test_critic.py) |
| 29 | Convergence guard (identical / no-progress stops early) | **COVERED** | `test_convergence_identical_revision_stops_early` (tests/test_critic.py) |
| 30 | Invalid-critic-output → default + iterate (§11) | **COVERED** | `test_invalid_critic_output_then_iterate` (tests/test_critic.py); `test_critic_failed_validation_defaults` (tests/test_models.py) |
| 31 | Final-answer critic pass (synthesized answer gated, §6.3) | **COVERED** | `test_synthesize_merges_and_passes_final_critic`, `test_final_critic_rejects_then_resynthesizes` (tests/test_synthesizer.py) |
| 32 | Synthesizer sees low-confidence input summary | **COVERED** | `test_synth_sees_degraded_input_summary` (tests/test_synthesizer.py) |
| 33 | Per-role rubric surfaces in critic | **`not yet confirmed`** | R4 could not supply a direct test function. `test_rubric_for_returns_role_criteria`, `test_critic_prompt_embeds_rubric` (tests/test_roles.py) test prompt construction, not the `critic_score` event payload. **No assertion found that rubric line-items arrive in the critic_score event.** |

### Cluster 5 — Augmentation + dependency data-flow (§14.1 bullets 5–6; items 22–27)
*(R5 cluster — re-confirmed by direct reads.)*

| # | Item | Verdict | Cited test(s) |
|---|------|---------|---------------|
| 22 | Missing-coder injection (wired to researcher) | **COVERED** | `test_missing_coder_injected_and_wired` (tests/test_augmentation.py) |
| 23 | Missing-synthesizer injection (wired to all) | **COVERED** | `test_missing_synthesizer_injected_and_wired` (tests/test_augmentation.py) |
| 24 | Dependency wiring (incl. "or all existing" branch) | **COVERED** | `test_missing_coder_injected_and_wired`, `test_missing_synthesizer_injected_and_wired`, `test_no_researcher_coder_depends_on_all` (tests/test_augmentation.py) |
| 25 | Post-augmentation acyclicity | **COVERED** | `test_post_augmentation_plan_validates` (tests/test_augmentation.py) |
| 26 | Dependency data-flow: upstream summary+artifacts injected | **COVERED** | `test_render_includes_upstream_summary_and_artifacts`, `test_render_caps_and_drops_lowest_confidence_first` (tests/test_worker.py) |
| 27 | Low-confidence flag propagated to dependents | **COVERED** | `test_render_flags_low_confidence_dependency` (tests/test_worker.py) |
| — | (bonus) augmentation idempotency | **COVERED** | `test_already_complete_plan_unchanged_idempotent` (tests/test_augmentation.py) |

### Cluster 6 — E2E / OTel / budgets (§14.2–§14.5; items 34–37)
*(R5 cluster — re-confirmed by direct reads.)*

| # | Item | Verdict | Cited test(s) |
|---|------|---------|---------------|
| 34 | End-to-end fixture run (real workspace mutated) | **COVERED** | `test_e2e_full_trace_redacted_and_silent` (tests/test_e2e_fixture.py); also `test_happy_path_end_to_end` (tests/test_orchestrator.py) |
| 35 | Complete trace tree + redaction | **COVERED** | event ordering + secret redaction in `test_e2e_full_trace_redacted_and_silent` (tests/test_e2e_fixture.py); span parenting in `test_span_hierarchy_run_subtask_tool`, `test_planner_llm_call_parents_to_run_span`, `test_crashed_run_still_closes_spans` (tests/test_otel.py); unit scrubbing in tests/test_redaction.py |
| 36 | Silent on stderr | **COVERED** | `capfd` assertion `captured.err == ""` in `test_e2e_full_trace_redacted_and_silent` (tests/test_e2e_fixture.py) |
| 37 | Global budget respect + `run_finished` | **COVERED** | `test_call_ceiling_triggers`, `test_token_ceiling_triggers`, `test_none_token_budget_never_triggers_on_tokens`, `test_time_ceiling_triggers` (tests/test_budget.py); integration `test_budget_exhausted_degrades_gracefully`, `test_planner_unrepairable_raises_but_emits_run_finished` (tests/test_orchestrator.py) |

---

## Prioritized MISSING / THIN items to add

In priority order (highest leverage first):

1. **THIN — item 18 (`env` argument guard).** Add `test_env_non_allowlisted_program_rejected`
   asserting that `env <non-allowlisted-program>` (e.g. `env nmap`) is refused
   the same way `xargs nmap` is. SPEC §7 item 5 names `xargs`/`env` together;
   only `xargs` is currently tested (`test_xargs_non_allowlisted_program_rejected`).
   This is the clearest, smallest concrete gap in the whole 37-item set.

2. **THIN — item 17 sub-vectors.** `test_git_dash_c_rejected` proves the primary
   `-c` vector, but `--exec-path`, git hooks, aliases, and pagers are not
   individually tested. Add a case per remaining vector for defense-in-depth
   confidence.

3. **NOT YET CONFIRMED — item 33 (rubric in `critic_score` event).** Add a test
   asserting that the critic's rubric line-items surface in the `critic_score`
   observer event fields (§9 event table, §6.1). Currently only prompt
   *construction* of the rubric is tested (`tests/test_roles.py`).

4. **THIN — item 11 named deny-patterns.** The deny-pattern *mechanism* is proven
   via `test_sudo_denied`, but `rm -rf /`, fork bombs, `chmod 777`, and
   `bash -c` are not asserted by name. Add table-driven cases for each
   explicitly listed SPEC §7 item-6 pattern.

---

## Verification

`git status --porcelain` was run after writing this report. The only file
this session created/modified is `docs/CRUCIBLE_TEST_GAP_REPORT.md`
(untracked `?? docs/CRUCIBLE_TEST_GAP_REPORT.md`). All other entries in the
porcelain output were pre-existing in the working tree before this session —
this writer did not touch any of them (the task forbade modifying any other
file, and `pytest` was not run). **Confirmed: the only created file is
`docs/CRUCIBLE_TEST_GAP_REPORT.md`.**
