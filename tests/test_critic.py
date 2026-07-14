import json

from orchestrator.config import Config
from orchestrator.critic import (
    enforce_threshold,
    is_accepted,
    run_critic_loop,
)
from orchestrator.llm import FakeLLMClient, llm_response
from orchestrator.models import CriticScore, WorkerResult
from orchestrator.observers import Observer
from orchestrator.worker import WorkerOutput


def _wo(summary, confidence=0.5):
    return WorkerOutput(result=WorkerResult(summary=summary, confidence=confidence), messages=[])


def _critic_json(score, approved):
    return llm_response(f'{{"score": {score}, "approved": {str(approved).lower()}, "issues": ["x"], "suggestions": ["y"]}}')


def _critic_json_with_rubric(score, approved, rubric):
    payload = {
        "score": score,
        "approved": approved,
        "issues": ["x"],
        "suggestions": ["y"],
        "rubric": rubric,
    }
    return llm_response(json.dumps(payload))


def _revise_fn_from(outputs):
    it = iter(outputs)

    async def revise(prev, score):
        return next(it)

    return revise


class RecordingObserver(Observer):
    def __init__(self):
        self.events = []   # event names, in order
        self.records = []  # (name, fields) pairs, for field-level assertions

    def __getattribute__(self, name):
        # Record every known event call.
        if name in (
            "run_started", "plan_ready", "subtask_started", "llm_call",
            "tool_call", "critic_score", "subtask_finished", "verify",
            "run_finished", "flush",
        ):
            def rec(**f):
                self.events.append(name)
                self.records.append((name, f))
            return rec
        return object.__getattribute__(self, name)


# --- pure gate logic ------------------------------------------------------
def test_is_accepted_requires_both():
    assert is_accepted(CriticScore(score=9, approved=True), 8)
    assert not is_accepted(CriticScore(score=9, approved=False), 8)  # rejection authoritative
    assert not is_accepted(CriticScore(score=7, approved=True), 8)   # below threshold


def test_enforce_threshold_demotes_high_score_below_threshold():
    s = enforce_threshold(CriticScore(score=7, approved=True), 8)
    assert s.approved is False


def test_enforce_threshold_keeps_rejection():
    s = enforce_threshold(CriticScore(score=9, approved=False), 8)
    assert s.approved is False


# --- the loop -------------------------------------------------------------
async def test_accepted_revision_is_kept_regression():
    # v1 discard bug: a revision that finally passes must be returned, not the
    # stale rejected version.
    fake = FakeLLMClient({"critic": [_critic_json(3, False), _critic_json(9, True)]})
    config = Config(workspace="/tmp", max_iterations_per_subtask=1)
    initial = _wo("v1-rejected")
    revise = _revise_fn_from([_wo("v2-accepted", confidence=0.9)])
    out, score = await run_critic_loop(
        fake, "coder", "c1", "task", initial, revise, config
    )
    assert out.result.summary == "v2-accepted"  # NOT 'v1-rejected'
    assert score.approved is True


async def test_threshold_gates_high_score():
    # Critic approves with score 7, but per-role threshold is 8 -> not accepted,
    # and (max_iterations=0) it stays unaccepted but returns the result.
    fake = FakeLLMClient({"critic": [_critic_json(7, True)]})
    config = Config(workspace="/tmp", max_iterations_per_subtask=0, approval_threshold=8)
    out, score = await run_critic_loop(
        fake, "coder", "c1", "task", _wo("x"), _revise_fn_from([]), config
    )
    assert score.approved is False  # demoted by threshold


async def test_critic_rejection_authoritative():
    fake = FakeLLMClient({"critic": [_critic_json(10, False)]})
    config = Config(workspace="/tmp", max_iterations_per_subtask=0)
    out, score = await run_critic_loop(
        fake, "coder", "c1", "task", _wo("x"), _revise_fn_from([]), config
    )
    assert not is_accepted(score, config.threshold_for("coder"))


async def test_convergence_identical_revision_stops_early():
    # Critic always rejects; revision is identical -> should break after 1 revise,
    # not burn the whole budget.
    fake = FakeLLMClient({"critic": [_critic_json(3, False)] * 10})
    config = Config(workspace="/tmp", max_iterations_per_subtask=5)
    revise = _revise_fn_from([_wo("same"), _wo("same"), _wo("same")])
    out, score = await run_critic_loop(
        fake, "coder", "c1", "task", _wo("same"), revise, config
    )
    # initial critic call + exactly one revision critic call = 2 calls total.
    critic_calls = [c for c in fake.calls if c["role"] == "critic"]
    assert len(critic_calls) == 2


async def test_invalid_critic_output_then_iterate():
    fake = FakeLLMClient({"critic": [llm_response("not json"), _critic_json(9, True)]})
    config = Config(workspace="/tmp", max_iterations_per_subtask=1)
    revise = _revise_fn_from([_wo("v2", confidence=0.9)])
    out, score = await run_critic_loop(
        fake, "coder", "c1", "task", _wo("v1"), revise, config
    )
    # First critic output failed validation (score 5, unapproved) -> iterate ->
    # second is accepted.
    assert score.approved is True
    assert out.result.summary == "v2"


# --- observer event capture (SPEC §6.1, §9) -------------------------------
async def test_critic_score_event_surfaces_rubric_line_items():
    # The critic_score event (§9) must carry the per-role rubric line-items
    # (§6.1) so a trace answers "which criteria passed?". Drive the loop with a
    # FakeLLMClient whose critic response embeds a rubric array, and capture the
    # emitted event via a RecordingObserver.
    rec = RecordingObserver()
    rubric = [
        {"criterion": "covers the spec", "passed": True, "note": "good"},
        {"criterion": "tests added", "passed": False, "note": "missing"},
    ]
    fake = FakeLLMClient({"critic": [_critic_json_with_rubric(9, True, rubric)]})
    config = Config(workspace="/tmp", max_iterations_per_subtask=0)
    out, score = await run_critic_loop(
        fake, "coder", "c1", "task", _wo("x"), _revise_fn_from([]), config,
        observer=rec,
    )
    critic_events = [f for name, f in rec.records if name == "critic_score"]
    assert len(critic_events) == 1  # one scoring round
    payload = critic_events[0]
    assert payload["subtask_id"] == "c1"
    assert payload["score"] == 9
    assert payload["approved"] is True  # RAW critic value (pre-threshold), here accepted
    assert payload["issues"] == ["x"]
    # The event surfaces the rubric line-items, each RubricItem.model_dump().
    assert payload["rubric"] == rubric
