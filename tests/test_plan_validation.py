import pytest

from orchestrator.errors import PlanValidationError
from orchestrator.models import Plan, Subtask
from orchestrator.plan_validation import topological_layers, validate_plan


def _p(*subtasks):
    return Plan(subtasks=list(subtasks))


def test_valid_plan_layers():
    plan = _p(
        Subtask(id="r1", role="researcher", task="t"),
        Subtask(id="c1", role="coder", task="t", depends_on=["r1"]),
        Subtask(id="s1", role="synthesizer", task="t", depends_on=["r1", "c1"]),
    )
    layers = validate_plan(plan)
    assert layers == [["r1"], ["c1"], ["s1"]]


def test_diamond_dag_layers():
    plan = _p(
        Subtask(id="a", role="researcher", task="t"),
        Subtask(id="b", role="researcher", task="t", depends_on=["a"]),
        Subtask(id="c", role="researcher", task="t", depends_on=["a"]),
        Subtask(id="d", role="coder", task="t", depends_on=["b", "c"]),
    )
    layers = validate_plan(plan)
    assert layers[0] == ["a"]
    assert sorted(layers[1]) == ["b", "c"]
    assert layers[2] == ["d"]


def test_duplicate_id_rejected():
    plan = _p(
        Subtask(id="x", role="researcher", task="t"),
        Subtask(id="x", role="coder", task="t"),
    )
    with pytest.raises(PlanValidationError, match="duplicate"):
        validate_plan(plan)


def test_bad_role_rejected():
    plan = _p(Subtask(id="x", role="wizard", task="t"))
    with pytest.raises(PlanValidationError, match="role"):
        validate_plan(plan)


def test_dangling_dep_names_id():
    plan = _p(Subtask(id="c3", role="coder", task="t", depends_on=["c9"]))
    with pytest.raises(PlanValidationError, match="c9"):
        validate_plan(plan)


def test_cycle_detected():
    plan = _p(
        Subtask(id="c1", role="coder", task="t", depends_on=["c2"]),
        Subtask(id="c2", role="coder", task="t", depends_on=["c1"]),
    )
    with pytest.raises(PlanValidationError, match="cycle"):
        validate_plan(plan)


def test_self_dependency_rejected():
    plan = _p(Subtask(id="c1", role="coder", task="t", depends_on=["c1"]))
    with pytest.raises(PlanValidationError, match="itself"):
        validate_plan(plan)


def test_empty_plan_rejected():
    with pytest.raises(PlanValidationError):
        validate_plan(Plan(subtasks=[]))
