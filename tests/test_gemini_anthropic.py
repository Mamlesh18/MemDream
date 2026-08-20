"""Gemini and Anthropic: the two providers that need their own SDK.

Everything else in MemDream speaks the OpenAI wire format and shares one
implementation. These two do not, and the reason is the same in both cases —
**guaranteed structured output**. Every Dream decision has to satisfy a Pydantic
schema, so a provider that can only be *asked* for JSON costs a repair round-trip
on every malformed reply:

    gemini      response_mime_type="application/json" is a decoding constraint,
                which the OpenAI-compatible shim does not reliably expose (nor
                does it always populate token usage, and cost is reported per
                operation here).
    anthropic   no JSON mode at all; the assistant turn is prefilled with an
                opening brace so the model can only continue from inside an
                object.

So the tests below concentrate on configuration resolution and on those two
mechanisms. The transport itself — retries, metrics, the repair round-trip — is
the same shape as the shared base and is covered by `test_llm_provider.py`.

The SDKs are optional extras, so anything needing one is skipped when it is not
installed rather than failing the suite.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.exceptions import ConfigurationError, ProviderError
from app.providers.embeddings.factory import build_embedding_provider
from app.providers.llm.factory import build_llm_provider

pytestmark = pytest.mark.asyncio


def _settings(**overrides) -> Settings:
    return Settings(env="test", identity_salt="t", **overrides)


# --------------------------------------------------------------------------- #
# Configuration — no SDK needed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("provider", ["gemini", "anthropic"])
async def test_both_are_selectable_as_llm_providers(provider):
    assert _settings(llm_provider=provider).llm_provider == provider


async def test_gemini_is_selectable_for_embeddings_but_anthropic_is_not():
    """Anthropic has no embeddings API, so it must not be offerable as one."""
    assert _settings(embedding_provider="gemini").embedding_provider == "gemini"

    with pytest.raises(ValueError):
        _settings(embedding_provider="anthropic")


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("gemini_chat_model", "gemini-2.0-flash"),
        ("gemini_embedding_model", "gemini-embedding-001"),
        ("anthropic_model", "claude-sonnet-4-5"),
    ],
)
async def test_defaults_name_a_real_model(field, expected):
    """So a deployment that sets only an API key still boots."""
    assert getattr(_settings(), field) == expected


async def test_gemini_embeddings_default_to_a_narrower_vector():
    """768 rather than the maximum: every write runs a similarity search, and a
    narrower vector is cheaper to store and faster to compare."""
    assert _settings().gemini_embedding_dimensions == 768


@pytest.mark.parametrize(
    ("env_var", "field"),
    [
        ("GOOGLE_API_KEY", "gemini_api_key"),
        ("GEMINI_API_KEY", "gemini_api_key"),
        ("DREAM_GEMINI_API_KEY", "gemini_api_key"),
        ("ANTHROPIC_API_KEY", "anthropic_api_key"),
        ("DREAM_ANTHROPIC_API_KEY", "anthropic_api_key"),
    ],
)
async def test_conventional_env_vars_are_read(monkeypatch, env_var, field):
    """Someone who already exported ANTHROPIC_API_KEY should not have to
    redeclare it under a DREAM_ prefix."""
    monkeypatch.setenv(env_var, "key-from-environment")
    settings = Settings(env="test", identity_salt="t")
    assert getattr(settings, field) == "key-from-environment"


@pytest.mark.parametrize("provider", ["gemini", "anthropic"])
async def test_factory_has_a_branch_for_each(provider):
    """A selectable provider with no factory branch is dead configuration. The
    call is expected to fail — no key, possibly no SDK — but never with
    an unknown-provider error."""
    with pytest.raises(ProviderError) as exc:
        build_llm_provider(_settings(llm_provider=provider))

    assert not isinstance(exc.value, ConfigurationError)


async def test_gemini_embedding_factory_has_a_branch():
    with pytest.raises(ProviderError) as exc:
        build_embedding_provider(_settings(embedding_provider="gemini"))

    assert not isinstance(exc.value, ConfigurationError)


@pytest.mark.parametrize(
    ("provider", "module", "expected_variable"),
    [
        ("gemini", "google.genai", "GOOGLE_API_KEY"),
        ("anthropic", "anthropic", "ANTHROPIC_API_KEY"),
    ],
)
async def test_a_missing_key_names_the_variable_to_set(provider, module, expected_variable):
    """The error a first-time user is most likely to hit, so it must say what
    to do rather than only what went wrong."""
    pytest.importorskip(module, reason=f"{provider} SDK not installed")

    with pytest.raises(ProviderError) as exc:
        build_llm_provider(_settings(llm_provider=provider))

    assert expected_variable in str(exc.value)


# --------------------------------------------------------------------------- #
# Gemini
# --------------------------------------------------------------------------- #


async def test_gemini_labels_itself_for_metrics_and_audit():
    pytest.importorskip("google.genai")
    from app.providers.llm.gemini import GeminiLLMProvider

    provider = GeminiLLMProvider(api_key="k", model="gemini-2.0-flash")
    # Metrics, audit rows and logs all key off this.
    assert provider.model_name == "gemini:gemini-2.0-flash"


async def test_gemini_embeddings_report_the_configured_width():
    pytest.importorskip("google.genai")
    from app.providers.embeddings.gemini import GeminiEmbeddingProvider

    provider = GeminiEmbeddingProvider(api_key="k", dimensions=1536)

    assert provider.dimensions == 1536
    assert provider.model_name == "gemini:gemini-embedding-001"


async def test_gemini_asks_for_json_as_a_decoding_constraint(monkeypatch):
    """The whole reason the native SDK is used instead of the OpenAI shim."""
    pytest.importorskip("google.genai")
    from app.providers.llm.gemini import GeminiLLMProvider

    provider = GeminiLLMProvider(api_key="k")
    captured: dict = {}

    async def fake_generate(*, model, contents, config):
        captured["config"] = config
        raise RuntimeError("stop here - only the request is under test")

    monkeypatch.setattr(provider._client.aio.models, "generate_content", fake_generate)

    with pytest.raises(ProviderError):
        await provider._call("system", "user", 0.1, 100)

    assert captured["config"].response_mime_type == "application/json"
    assert captured["config"].system_instruction == "system"


async def test_gemini_embeddings_are_normalised(monkeypatch):
    """Matryoshka-truncated vectors are not unit length, and every vector store
    here uses inner product assuming they are — so an un-normalised vector would
    quietly distort every similarity score."""
    pytest.importorskip("google.genai")
    from app.providers.embeddings.gemini import GeminiEmbeddingProvider

    provider = GeminiEmbeddingProvider(api_key="k", dimensions=3)

    class _Embedding:
        values = [3.0, 4.0, 0.0]  # length 5, deliberately not unit

    class _Response:
        embeddings = [_Embedding()]
        metadata = None

    async def fake_embed(*, model, contents, config):
        return _Response()

    monkeypatch.setattr(provider._client.aio.models, "embed_content", fake_embed)

    vector = await provider.embed("anything")

    assert vector == pytest.approx([0.6, 0.8, 0.0])


async def test_gemini_says_when_it_returned_nothing(monkeypatch):
    """A safety block produces an empty response. Saying so beats a
    no-JSON-found error several frames further down."""
    pytest.importorskip("google.genai")
    from app.core.exceptions import LLMResponseError
    from app.providers.llm.gemini import GeminiLLMProvider

    provider = GeminiLLMProvider(api_key="k")

    class _Blocked:
        text = ""
        usage_metadata = None
        prompt_feedback = "BLOCKED_SAFETY"

    async def fake_generate(*, model, contents, config):
        return _Blocked()

    monkeypatch.setattr(provider._client.aio.models, "generate_content", fake_generate)

    with pytest.raises(LLMResponseError, match="BLOCKED_SAFETY"):
        await provider._call("system", "user", 0.1, 100)


# --------------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------------- #


async def test_anthropic_labels_itself_for_metrics_and_audit():
    pytest.importorskip("anthropic")
    from app.providers.llm.anthropic import AnthropicLLMProvider

    provider = AnthropicLLMProvider(api_key="k", model="claude-sonnet-4-5")
    assert provider.model_name == "anthropic:claude-sonnet-4-5"
    await provider.close()


async def test_anthropic_prefills_the_assistant_turn_with_a_brace(monkeypatch):
    """Claude has no JSON mode. Prefilling the assistant turn with an opening
    brace leaves it no way to begin with prose — the same guarantee by a
    different mechanism, and the reason this provider is not just another
    OpenAI-compatible preset."""
    pytest.importorskip("anthropic")
    from pydantic import BaseModel

    from app.providers.llm.anthropic import AnthropicLLMProvider

    class Decision(BaseModel):
        decision: str

    provider = AnthropicLLMProvider(api_key="k")
    captured: dict = {}

    class _Usage:
        input_tokens = 10
        output_tokens = 5

    class _Block:
        type = "text"
        # Note the missing opening brace: the model continues from the prefill.
        text = '"decision": "NOOP"}'

    class _Response:
        content = [_Block()]
        usage = _Usage()
        stop_reason = "end_turn"

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr(provider._client.messages, "create", fake_create)

    result = await provider.generate_json(
        system="decide", user="I like biryani.", response_model=Decision
    )

    assert captured["messages"][-1] == {"role": "assistant", "content": "{"}
    # The brace is put back before parsing, so the half-object still validates.
    assert result.data.decision == "NOOP"
    assert result.repaired is False
    assert result.usage["prompt_tokens"] == 10
    assert result.usage["total_tokens"] == 15
    await provider.close()


async def test_anthropic_repairs_a_response_that_fails_validation(monkeypatch):
    """One repair round-trip, with the validation error fed back — and the
    tokens from both calls counted, since cost is reported per operation."""
    pytest.importorskip("anthropic")
    from pydantic import BaseModel

    from app.providers.llm.anthropic import AnthropicLLMProvider

    class Decision(BaseModel):
        decision: str

    provider = AnthropicLLMProvider(api_key="k")
    calls: list[dict] = []

    class _Usage:
        input_tokens = 10
        output_tokens = 5

    def _response(text: str):
        block = type("_Block", (), {"type": "text", "text": text})()
        return type(
            "_Response",
            (),
            {"content": [block], "usage": _Usage(), "stop_reason": "end_turn"},
        )()

    async def fake_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _response('"wrong_field": 1}')
        return _response('"decision": "ADD"}')

    monkeypatch.setattr(provider._client.messages, "create", fake_create)

    result = await provider.generate_json(
        system="decide", user="I like biryani.", response_model=Decision
    )

    assert result.repaired is True
    assert result.data.decision == "ADD"
    # The failed attempt is still billed, so it is still counted.
    assert result.usage["total_tokens"] == 30
    # The repair turn is prefilled too, otherwise the model could apologise
    # in prose instead of correcting the object.
    assert calls[1]["messages"][-1] == {"role": "assistant", "content": "{"}
    await provider.close()


async def test_anthropic_names_truncation_rather_than_letting_json_fail(monkeypatch):
    """max_tokens truncation produces unterminated JSON. Naming the cause is
    actionable; a parse error at character 811 is not."""
    pytest.importorskip("anthropic")
    from app.core.exceptions import LLMResponseError
    from app.providers.llm.anthropic import AnthropicLLMProvider

    provider = AnthropicLLMProvider(api_key="k")

    class _Truncated:
        content = []
        usage = None
        stop_reason = "max_tokens"

    async def fake_create(**kwargs):
        return _Truncated()

    monkeypatch.setattr(provider._client.messages, "create", fake_create)

    with pytest.raises(LLMResponseError, match="MAX_OUTPUT_TOKENS"):
        await provider._call("system", [{"role": "user", "content": "hi"}], 0.1, 100)

    await provider.close()
