"""Provider registry: which model backends exist and how to reach them.

Each provider names its API-key env var(s), endpoint, default model, and
client kind. GLM and MiniMax speak the OpenAI Chat Completions protocol
(served by ``OpenAIClient``); Anthropic uses the official ``anthropic`` SDK
(``AnthropicClient``) — per Anthropic's guidance, never an OpenAI-compat shim.

Resolution order (``resolve_from_env``):
  1. ``LLM_API_KEY`` set → the explicit bring-your-own-endpoint escape hatch
     (``LLM_BASE_URL`` / ``LLM_MODEL``), exactly as before.
  2. Otherwise the first provider in ``PROVIDER_ORDER`` whose key env is set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .llm import LLMClient, OpenAIClient


@dataclass(frozen=True)
class Provider:
    name: str
    api_key_envs: tuple[str, ...]  # checked in order for the key
    base_url: str | None  # None → native SDK endpoint (anthropic)
    default_model: str  # "" → model must be given explicitly
    kind: str  # "openai" | "anthropic"
    notes: str = ""


PROVIDERS: dict[str, Provider] = {
    "glm": Provider(
        name="glm",
        api_key_envs=("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
        # GLM Coding Plan endpoint (docs.z.ai/devpack) — the coding-only path,
        # NOT the general /api/paas/v4. Same value the musa driver validated.
        base_url="https://api.z.ai/api/coding/paas/v4",
        default_model="glm-5.2",
        kind="openai",
        notes="z.ai GLM Coding Plan; glm-5.2 is the proven-green build model",
    ),
    "minimax": Provider(
        name="minimax",
        api_key_envs=("MINIMAX_API_KEY",),
        base_url="https://api.minimax.io/v1",
        default_model="MiniMax-M3",
        kind="openai",
        notes="MiniMax-M3 is the proven-green build model",
    ),
    "anthropic": Provider(
        name="anthropic",
        api_key_envs=("ANTHROPIC_API_KEY",),
        base_url=None,  # official anthropic SDK, native Messages API
        default_model="claude-opus-4-8",
        kind="anthropic",
        notes="official SDK; also resolves `ant auth login` profiles when the env key is unset",
    ),
    "openai": Provider(
        name="openai",
        api_key_envs=("OPENAI_API_KEY",),
        base_url="https://api.openai.com/v1",
        default_model="",  # no default on purpose: pick explicitly via /model openai <name>
        kind="openai",
        notes="needs a key AND an explicit model",
    ),
}

PROVIDER_ORDER = ("glm", "minimax", "anthropic", "openai")


def api_key_for(provider: Provider) -> str | None:
    for env in provider.api_key_envs:
        val = os.environ.get(env)
        if val:
            return val
    return None


def available_providers() -> list[Provider]:
    """Providers whose API key is present, in preference order."""
    return [PROVIDERS[n] for n in PROVIDER_ORDER if api_key_for(PROVIDERS[n])]


@dataclass(frozen=True)
class ResolvedProvider:
    provider: Provider | None  # None → explicit LLM_* env (custom endpoint)
    api_key: str | None
    base_url: str | None
    model: str

    @property
    def label(self) -> str:
        return self.provider.name if self.provider else "custom"


def resolve_from_env() -> ResolvedProvider | None:
    """Pick the backend from the environment; None when nothing is configured."""
    explicit = os.environ.get("LLM_API_KEY")
    if explicit:
        return ResolvedProvider(
            provider=None,
            api_key=explicit,
            base_url=os.environ.get("LLM_BASE_URL"),
            model=os.environ.get("LLM_MODEL", "default"),
        )
    for provider in available_providers():
        return ResolvedProvider(
            provider=provider,
            api_key=api_key_for(provider),
            base_url=provider.base_url,
            model=os.environ.get("LLM_MODEL") or provider.default_model,
        )
    return None


def resolve_provider(name: str, model: str | None = None) -> ResolvedProvider:
    """Resolve a named provider (for /model switching). Raises KeyError on an
    unknown name and ValueError when a model is required but not given."""
    provider = PROVIDERS[name]
    chosen = model or provider.default_model
    if not chosen:
        raise ValueError(f"provider {name!r} has no default model — specify one")
    return ResolvedProvider(
        provider=provider,
        api_key=api_key_for(provider),
        base_url=provider.base_url,
        model=chosen,
    )


def make_client(resolved: ResolvedProvider) -> LLMClient:
    """Build the right client for a resolved provider."""
    if resolved.provider is not None and resolved.provider.kind == "anthropic":
        from .anthropic_client import AnthropicClient  # lazy: optional heavy import

        # api_key may be None: the SDK then resolves ANTHROPIC_AUTH_TOKEN or an
        # `ant auth login` profile on its own.
        return AnthropicClient(api_key=resolved.api_key, model=resolved.model)
    return OpenAIClient(resolved.api_key, resolved.base_url, resolved.model)
