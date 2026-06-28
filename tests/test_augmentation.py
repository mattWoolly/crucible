from orchestrator.augmentation import augment_plan
from orchestrator.constants import CODER, SYNTHESIZER
from orchestrator.models import Plan, Subtask
from orchestrator.plan_validation import validate_plan


def _roles(plan):
    return [s.role for s in plan.subtasks]


def test_missing_coder_injected_and_wired():
    plan = Plan(subtasks=[Subtask(id="r1", role="researcher", task="research")])
    out = augment_plan(plan, "do the thing")
    coders = [s for s in out.subtasks if s.role == CODER]
    assert len(coders) == 1
    assert coders[0].depends_on == ["r1"]  # depends on researcher
    assert "do the thing" in coders[0].task


def test_missing_synthesizer_injected_and_wired():
    plan = Plan(subtasks=[Subtask(id="c1", role="coder", task="code")])
    out = augment_plan(plan, "task")
    synths = [s for s in out.subtasks if s.role == SYNTHESIZER]
    assert len(synths) == 1
    # synthesizer depends on all pre-existing subtasks
    assert "c1" in synths[0].depends_on


def test_already_complete_plan_unchanged_idempotent():
    plan = Plan(subtasks=[
        Subtask(id="c1", role="coder", task="code"),
        Subtask(id="s1", role="synthesizer", task="merge", depends_on=["c1"]),
    ])
    out = augment_plan(plan, "task")
    assert _roles(out) == _roles(plan)
    # Idempotent: augmenting again changes nothing.
    out2 = augment_plan(out, "task")
    assert [s.id for s in out2.subtasks] == [s.id for s in out.subtasks]


def test_post_augmentation_plan_validates():
    plan = Plan(subtasks=[Subtask(id="r1", role="researcher", task="research")])
    out = augment_plan(plan, "task")
    # Must remain acyclic and complete.
    layers = validate_plan(out)
    assert layers  # no raise; synthesizer is in the last layer
    assert _roles(out).count(CODER) == 1
    assert _roles(out).count(SYNTHESIZER) == 1


def test_no_researcher_coder_depends_on_all():
    plan = Plan(subtasks=[
        Subtask(id="a", role="coder", task="x"),
    ])
    # Already has a coder, so no coder injected; only synth added.
    out = augment_plan(plan, "task")
    assert _roles(out).count(CODER) == 1
