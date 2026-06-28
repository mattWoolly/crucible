from orchestrator.config import Config, LLMSettings
from orchestrator.constants import CODER, PLANNER, RESEARCHER


def test_defaults():
    c = Config(workspace="/ws")
    assert c.approval_threshold == 8.0
    assert c.max_iterations_per_subtask == 1
    assert c.max_steps == 8
    assert c.max_llm_calls == 200
    assert c.max_total_tokens is None
    assert c.run_timeout_s == 600.0


def test_per_role_threshold_override():
    c = Config(workspace="/ws", approval_threshold_by_role={CODER: 9.0})
    assert c.threshold_for(CODER) == 9.0
    assert c.threshold_for(RESEARCHER) == 8.0  # falls back to base


def test_temperature_for_falls_back_to_default():
    c = Config(workspace="/ws")
    assert c.temperature_for(PLANNER) == 0.0
    assert c.temperature_for(RESEARCHER) == 0.2  # default bucket


def test_llm_settings_from_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "MiniMax-Text-01")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.minimax.io/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    s = LLMSettings.from_env()
    assert s.model == "MiniMax-Text-01"
    assert s.base_url.endswith("/v1")
    assert s.api_key == "sk-test"


def test_config_role_temperature_is_independent_copy():
    c1 = Config(workspace="/a")
    c2 = Config(workspace="/b")
    c1.role_temperature["planner"] = 0.9
    assert c2.role_temperature["planner"] == 0.0
