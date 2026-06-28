from orchestrator.constants import DEGRADED_CONFIDENCE
from orchestrator.models import (
    CriticScore,
    FinalReport,
    Plan,
    Subtask,
    WorkerResult,
)


def test_degraded_uses_named_constant():
    r = WorkerResult.degraded("bad json", raw="x" * 1000)
    assert r.confidence == DEGRADED_CONFIDENCE
    assert r.is_degraded
    assert len(r.summary) == 500
    assert r.uncertainties == ["parse_failed: bad json"]
    assert r.artifacts == {}


def test_non_degraded_result_not_flagged():
    r = WorkerResult(summary="ok", confidence=0.8)
    assert not r.is_degraded


def test_critic_failed_validation_defaults():
    s = CriticScore.failed_validation()
    assert s.score == 5.0
    assert s.approved is False
    assert s.issues == ["critic output failed validation"]


def test_subtask_defaults():
    st = Subtask(id="a", role="researcher", task="do x")
    assert st.depends_on == []
    assert st.inputs == ""


def test_models_json_round_trip():
    plan = Plan(reasoning="r", subtasks=[Subtask(id="a", role="coder", task="t")])
    assert Plan.model_validate(plan.model_dump()) == plan

    report = FinalReport(
        summary="done",
        confidence=0.9,
        subtask_results={"a": WorkerResult(summary="s", confidence=0.7)},
        critic_scores={"a": CriticScore(score=9, approved=True)},
        iterations=2,
        tokens_total=123,
    )
    assert FinalReport.model_validate(report.model_dump()) == report
