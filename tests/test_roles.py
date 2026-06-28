from orchestrator.constants import CODER, RESEARCHER, SYNTHESIZER, WORKER_ROLES
from orchestrator.roles import (
    critic_prompt,
    rubric_for,
    system_prompt,
)


def test_every_worker_role_has_prompt():
    for role in WORKER_ROLES:
        assert system_prompt(role)


def test_rubric_for_returns_role_criteria():
    assert "cited" in rubric_for(RESEARCHER)
    assert "run_shell" in rubric_for(CODER)
    assert "worker outputs" in rubric_for(SYNTHESIZER)


def test_critic_prompt_embeds_rubric():
    p = critic_prompt(CODER)
    assert "CRITIC" in p
    assert "run_shell" in p  # coder rubric is embedded


def test_prompts_are_short():
    # ~200 token budget; assert a generous char ceiling as a proxy.
    for role in WORKER_ROLES:
        assert len(system_prompt(role)) < 1400
