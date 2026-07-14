"""Provider registry and env resolution (orchestrator/providers.py)."""

import pytest

from orchestrator.llm import OpenAIClient
from orchestrator.providers import (
    PROVIDER_ORDER,
    PROVIDERS,
    ResolvedProvider,
    api_key_for,
    available_providers,
    make_client,
    resolve_from_env,
    resolve_provider,
)

ALL_KEY_ENVS = [env for p in PROVIDERS.values() for env in p.api_key_envs] + [
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for env in ALL_KEY_ENVS:
        monkeypatch.delenv(env, raising=False)


def test_no_keys_resolves_to_none():
    assert available_providers() == []
    assert resolve_from_env() is None


def test_explicit_llm_env_wins_over_providers(monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "glm-key")
    monkeypatch.setenv("LLM_API_KEY", "custom-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://my.endpoint/v1")
    monkeypatch.setenv("LLM_MODEL", "my-model")
    r = resolve_from_env()
    assert r.provider is None and r.label == "custom"
    assert r.api_key == "custom-key"
    assert r.base_url == "https://my.endpoint/v1"
    assert r.model == "my-model"


def test_provider_preference_order(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("MINIMAX_API_KEY", "m")
    monkeypatch.setenv("GLM_API_KEY", "g")
    r = resolve_from_env()
    assert r.label == "glm"  # glm first in PROVIDER_ORDER
    assert r.model == "glm-5.2"
    assert r.base_url == "https://api.z.ai/api/coding/paas/v4"


def test_glm_key_env_fallbacks(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "via-zai")
    assert api_key_for(PROVIDERS["glm"]) == "via-zai"


def test_anthropic_resolution(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    r = resolve_from_env()
    assert r.label == "anthropic"
    assert r.model == "claude-opus-4-8"
    assert r.base_url is None  # native SDK, no OpenAI-compat endpoint


def test_llm_model_overrides_provider_default(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "m")
    monkeypatch.setenv("LLM_MODEL", "MiniMax-M2")
    assert resolve_from_env().model == "MiniMax-M2"


def test_resolve_provider_by_name(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "m")
    r = resolve_provider("minimax")
    assert r.model == "MiniMax-M3" and r.api_key == "m"
    r2 = resolve_provider("minimax", "MiniMax-M2")
    assert r2.model == "MiniMax-M2"


def test_openai_requires_explicit_model():
    with pytest.raises(ValueError, match="no default model"):
        resolve_provider("openai")
    assert resolve_provider("openai", "some-model").model == "some-model"


def test_unknown_provider_raises_keyerror():
    with pytest.raises(KeyError):
        resolve_provider("nope")


def test_make_client_openai_kind():
    r = ResolvedProvider(provider=PROVIDERS["glm"], api_key="k",
                         base_url=PROVIDERS["glm"].base_url, model="glm-5.2")
    assert isinstance(make_client(r), OpenAIClient)


def test_make_client_anthropic_kind(monkeypatch):
    from orchestrator.anthropic_client import AnthropicClient
    r = ResolvedProvider(provider=PROVIDERS["anthropic"], api_key="sk-ant",
                         base_url=None, model="claude-opus-4-8")
    client = make_client(r)
    assert isinstance(client, AnthropicClient)
    assert client.model == "claude-opus-4-8"


def test_every_provider_in_order_exists():
    assert set(PROVIDER_ORDER) == set(PROVIDERS)
