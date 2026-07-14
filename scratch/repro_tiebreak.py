"""Reproduce the equal-confidence tie-break bug in `render_user_task`.

Contract (from `orchestrator/worker.py`, render_user_task docstring + inline
comment, lines 60-68):

    The block is capped; lowest-confidence-then-OLDEST dependencies are dropped
    first to stay under `cap`.
    ...
    # Drop lowest-confidence (tie: oldest) until under cap.
    worst = min(range(len(kept)), key=lambda i: (kept[i][1], -kept[i][2]))

`kept[i]` is a (text, confidence, order) tuple where `order` is the
dependency's 0-based position in `subtask.depends_on` (oldest = lowest order).

When two deps share the same confidence (a tie), the documented behavior is
"drop oldest first" — i.e. the one with the LOWEST `order` should be removed
first. The tie-break key is `-kept[i][2]`; `min` then picks the SMALLEST key.
For the OLDEST dep (smallest order), `-order` is the LARGEST, so `min` will
NEVER pick it — it instead drops the NEWEST (highest order) dep. The tie-break
is inverted: it drops newest-first instead of oldest-first.

This script builds 3 dependencies with EQUAL confidence, forces exactly one to
be dropped via the cap, and asserts which one survives.

Expected (per contract): the OLDEST dep (`d0`) is dropped; `d1`, `d2` survive.
Actual (bug): the NEWEST dep (`d2`) is dropped; `d0`, `d1` survive.
"""

from orchestrator.models import Subtask, WorkerResult
from orchestrator.worker import render_user_task


def main() -> int:
    # Three deps of EQUAL confidence, distinct orders (oldest -> newest).
    # Sizable summaries so a tight cap forces exactly one drop.
    n = 60
    deps = {
        "d0": WorkerResult(summary="ZERO " * n, confidence=0.5),   # order 0 (oldest)
        "d1": WorkerResult(summary="ONE " * n, confidence=0.5),    # order 1
        "d2": WorkerResult(summary="TWO " * n, confidence=0.5),    # order 2 (newest)
    }
    sub = Subtask(id="c", role="coder", task="t", depends_on=["d0", "d1", "d2"])

    # cap large enough to keep 2 blocks, small enough to force 1 drop.
    # Each block body (after the "### From `dN`" header + summary + artifacts)
    # is ~300+ chars; pick a cap that fits exactly two.
    cap = 720
    text = render_user_task(sub, deps, cap=cap)

    kept = [k for k in ("d0", "d1", "d2") if f"`{k}`" in text]
    dropped = [k for k in ("d0", "d1", "d2") if f"`{k}`" not in text]

    print(f"cap={cap}")
    print(f"kept   = {kept}")
    print(f"dropped = {dropped}")
    print()
    print("Contract: on an equal-confidence tie, drop the OLDEST dep (d0) first.")
    print()

    # Contract: OLDEST (d0) must be the one dropped; d1, d2 survive.
    expected_dropped = ["d0"]
    if dropped == expected_dropped:
        print("RESULT: PASS  (contract honored — oldest dropped first on tie)")
        return 0
    print(f"RESULT: FAIL  (contract VIOLATED — expected {expected_dropped} dropped, got {dropped})")
    print("        The inverted tie-break key `-order` made min() drop the NEWEST dep instead.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
