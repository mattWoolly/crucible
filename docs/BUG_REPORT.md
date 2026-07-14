# Bug Report: `render_user_task` inverts the equal-confidence tie-break (drops newest-first, not oldest-first)

## Summary

`orchestrator/worker.py::render_user_task` documents — in both its docstring and an inline comment — that when the "Context from prior steps" block must be trimmed to stay under `cap`, dependencies are dropped in order of **lowest confidence, then oldest first** (i.e. on a confidence tie, the dependency that appeared earliest in `subtask.depends_on` is dropped first).

The implementation does the opposite: **on an equal-confidence tie it drops the NEWEST dependency first.** The tie-break key in the `min(...)` selection is `-kept[i][2]` where index 2 is `order`. Negating `order` and taking `min` causes it to favor the lowest `-order` (= highest `order` = newest), so the newest dep is always selected for removal. The tie-break sign is inverted; it should be `+order` (or the block should drop the max, not the min).

## Location

`orchestrator/worker.py`, function `render_user_task`.

- Inline comment, line 61:
  ```python
  # Drop lowest-confidence (tie: oldest) until under cap.
  ```
- Offending selection, line 68:
  ```python
  worst = min(range(len(kept)), key=lambda i: (kept[i][1], -kept[i][2]))
  ```
  `kept[i]` is a `(text, confidence, order)` tuple; `order` is the dependency's 0-based position in `subtask.depends_on` (oldest = smallest `order`). The intended tie-break is "oldest first", i.e. smallest `order`. The key `-kept[i][2]` flips the ordering, so `min` never picks the oldest dep on a tie — it picks the newest.

## Evidence

### 1. Reproduction script (executed) — `scratch/repro_tiebreak.py`

Three dependencies of EQUAL confidence (`0.5`), distinct orders (`d0` oldest, `d1`, `d2` newest); a cap that forces exactly one drop. Contract: `d0` (oldest) must be dropped.

Command and verbatim output:

```
$ uv run python scratch/repro_tiebreak.py
cap=720
kept   = ['d0', 'd1']
dropped = ['d2']

Contract: on an equal-confidence tie, drop the OLDEST dep (d0) first.

RESULT: FAIL  (contract VIOLATED — expected ['d0'] dropped, got ['d2'])
        The inverted tie-break key `-order` made min() drop the NEWEST dep instead.
```

The script exits non-zero (the reproduction is deterministic and the failure is reproducible). It demonstrates the documented contract is violated: the NEWEST dep (`d2`) is dropped instead of the OLDEST (`d0`).

### 2. Existing worker suite (executed) — baseline, currently green, no tie-break coverage

```
$ uv run pytest -q tests/test_worker.py
...........                                                              [100%]
11 passed in 1.57s
```

The suite is green but **does not cover the equal-confidence tie-break ordering**. The only cap/drop test, `test_render_caps_and_drops_lowest_confidence_first`, exercises a *confidence-difference* drop (a `0.2` block dropped before a `0.9` block), never an equal-confidence tie. There is no test asserting *which* of two equal-confidence deps is dropped, so this inversion is invisible to CI.

## Root cause

The tie-break comparator is sign-inverted. The selection uses `min(..., key=lambda i: (confidence, -order))`. For the documented "oldest first" drop order, the primary key (lowest confidence) is correct, but the secondary key must be `+order` (lowest `order` = oldest, dropped first). Using `-order` makes the secondary key *decreasing* in `order`, so on a tie `min` selects the **highest** `order` (newest) for removal — the exact reverse of the contract.

Equivalently: the code selects for removal the dependency it should *keep* on a tie.

## Proposed solution

Fix the tie-break sign so the OLDEST dependency is dropped first on a confidence tie.

**Before** (`orchestrator/worker.py`, line 68):
```python
worst = min(range(len(kept)), key=lambda i: (kept[i][1], -kept[i][2]))
```

**After:**
```python
worst = min(range(len(kept)), key=lambda i: (kept[i][1], kept[i][2]))
```

(Remove the leading `-` from `kept[i][2]`; `order` is already ascending-oldest, which is exactly the drop priority the contract specifies.) The single-block hard-truncate branch that follows is unaffected and correct.

### Proposed regression test (for `tests/test_worker.py`)

```python
def test_render_tie_break_drops_oldest_first_on_equal_confidence():
    # Equal confidence -> contract: drop the OLDEST dep first.
    sub = Subtask(id="c", role="coder", task="t", depends_on=["d0", "d1", "d2"])
    deps = {
        "d0": WorkerResult(summary="ZERO " * 60, confidence=0.5),  # oldest
        "d1": WorkerResult(summary="ONE " * 60, confidence=0.5),
        "d2": WorkerResult(summary="TWO " * 60, confidence=0.5),   # newest
    }
    text = render_user_task(sub, deps, cap=720)
    # cap fits exactly two blocks -> exactly one dropped; it must be d0.
    assert "`d1`" in text and "`d2`" in text
    assert "`d0`" not in text  # oldest dropped first on the tie
```

## Verification plan

1. Confirm `scratch/repro_tiebreak.py` currently prints `RESULT: FAIL` and exits 1 (done above).
2. Apply the one-character fix (remove `-` from `-kept[i][2]`).
3. Re-run `uv run python scratch/repro_tiebreak.py` → expect `RESULT: PASS` and exit 0.
4. Add the regression test above and run `uv run pytest -q tests/test_worker.py` → expect 12 passed.
5. Confirm the existing confidence-difference test (`test_render_caps_and_drops_lowest_confidence_first`) still passes — the primary key is unchanged, so it must.

## Scope note

No source under `orchestrator/` was modified to produce this report. `scratch/repro_tiebreak.py` (a new diagnostic) and this report (`docs/BUG_REPORT.md`) are the only new files.
