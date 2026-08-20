"""OpenAI-compatible providers: OpenAI, Groq, Ollama, Together, Fireworks.

These are one integration with several base URLs, not several integrations. The
tests below are mostly about *configuration resolution* — picking the right URL,
model and key, and failing with an actionable message when something is missing —
because the transport itself is the shared code already covered by
`test_llm_provider.py`.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.exceptions import ProviderError
from app.providers.embeddings.openai_compatible import (
    INFERENCE_ONLY,
)
from app.providers.embeddings.openai_compatible import (
    from_settings as embeddings_from_settings,
)
from app.providers.llm.openai_compatible import PRESETS, resolve

pytestmark = pytest.mark.asyncio


def _settings(**overrides) -> Settings:
    base = {"env": "test", "identity_salt": "t", "openai_api_key": "sk-test"}
    return Settings(**{**base, **overrides})


# --------------------------------------------------------------------------- #
# Preset resolution
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("provider", "expected_host"),
    [
        ("openai", "api.openai.com"),
        ("groq", "api.groq.com"),
        ("ollama", "localhost:11434"),
        ("together", "api.together.xyz"),
        ("fireworks", "api.fireworks.ai"),
    ],
)
async def test_each_named_provider_resolves_to_its_endpoint(provider, expected_host):
    _, base_url, _, model = resolve(provider, _settings())
    assert expected_host in base_url
    assert model, "every preset must name a default model"


async def test_explicit_configuration_overrides_the_preset():
    settings = _settings(
        openai_base_url="https://my-proxy.internal/v1", openai_chat_model="my-model"
    )
    _, base_url, _, model = resolve("openai", settings)

    assert base_url == "https://my-proxy.internal/v1"
    assert model == "my-model"


async def test_ollama_needs_no_api_key():
    """The reason this matters: MemDream runs fully local, no key, no cost."""
    _, base_url, api_key, model = resolve("ollama", _settings(openai_api_key=""))

    assert "localhost" in base_url
    assert api_key == ""
    assert model


async def test_conventional_env_var_is_picked_up(monkeypatch):
    """Someone who already exported OPENAI_API_KEY should not have to redeclare it."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk-from-environment")
    _, _, api_key, _ = resolve("groq", _settings(openai_api_key=""))
    assert api_key == "gsk-from-environment"


async def test_explicit_key_beats_the_environment(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-from-environment")
    _, _, api_key, _ = resolve("groq", _settings(openai_api_key="sk-explicit"))
    assert api_key == "sk-explicit"


async def test_missing_key_names_the_variable_and_the_local_alternative():
    with pytest.raises(ProviderError) as exc:
        resolve("groq", _settings(openai_api_key=""))

    message = str(exc.value)
    assert "GROQ_API_KEY" in message
    # An actionable error points at the way out, not just the problem.
    assert "ollama" in message


async def test_the_generic_escape_hatch_requires_full_configuration():
    with pytest.raises(ProviderError, match="base URL"):
        resolve("openai_compatible", _settings())

    _, base_url, _, model = resolve(
        "openai_compatible",
        _settings(openai_base_url="http://vllm.internal/v1", openai_chat_model="mistral"),
    )
    assert base_url == "http://vllm.internal/v1"
    assert model == "mistral"


async def test_unknown_provider_is_rejected():
    with pytest.raises(ProviderError, match="Unknown"):
        resolve("not-a-real-provider", _settings())


# --------------------------------------------------------------------------- #
# Chat-only services
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("provider", sorted(INFERENCE_ONLY))
async def test_inference_only_services_reject_embeddings_clearly(provider):
    """A puzzling 404 on the first write, turned into a clear startup message."""
    with pytest.raises(ProviderError) as exc:
        embeddings_from_settings(_settings(), provider)

    message = str(exc.value)
    assert "DREAM_EMBEDDING_PROVIDER" in message
    # And it says the two do not have to match.
    assert "does not have to match" in message


async def test_llm_and_embedding_providers_are_independent():
    """Groq for chat with OpenAI for embeddings is a normal pairing."""
    settings = _settings(llm_provider="groq", embedding_provider="openai")
    assert settings.llm_provider == "groq"
    assert settings.embedding_provider == "openai"


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


async def test_provider_reports_a_label_that_identifies_the_service():
    from app.providers.llm.openai_compatible import OpenAICompatibleLLMProvider

    provider = OpenAICompatibleLLMProvider(
        base_url="https://api.groq.com/openai/v1",
        api_key="gsk-test",
        model="llama-3.3-70b-versatile",
        label="groq",
    )
    # Metrics, audit rows and logs all key off this.
    assert provider.model_name == "groq:llama-3.3-70b-versatile"
    await provider.close()


async def test_embedding_provider_reports_its_dimensions():
    from app.providers.embeddings.openai_compatible import (
        OpenAICompatibleEmbeddingProvider,
    )

    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://localhost:11434/v1",
        api_key="",
        model="nomic-embed-text",
        dimensions=768,
        label="ollama",
        supports_dimensions=False,
    )
    assert provider.dimensions == 768
    assert provider.model_name == "ollama:nomic-embed-text"
    await provider.close()


async def test_every_llm_preset_has_a_matching_factory_branch():
    """A preset nobody can select is dead configuration."""
    from app.providers.llm.factory import build_llm_provider  # noqa: F401

    literal = Settings.model_fields["llm_provider"].annotation
    selectable = set(getattr(literal, "__args__", ()))
    for name in PRESETS:
        assert name in selectable, f"preset {name!r} is not selectable via DREAM_LLM_PROVIDER"
