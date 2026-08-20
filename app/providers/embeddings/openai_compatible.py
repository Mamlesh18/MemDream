"""Embeddings from OpenAI, Ollama, or any OpenAI-compatible endpoint.

Same one-integration-many-services reasoning as the LLM counterpart, with one
important difference: **not every chat provider does embeddings.** Groq and
Fireworks serve inference only. Rather than fail with a confusing 404 at the
first write, those presets are absent here and the factory says so plainly.

That asymmetry is why LLM and embedding providers are configured separately —
`DREAM_LLM_PROVIDER=groq` with `DREAM_EMBEDDING_PROVIDER=openai` is a perfectly
normal combination, and a common one, since Groq is fast and cheap for chat
while embeddings come from elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.core.exceptions import ProviderError
from app.core.logging import get_logger
from app.providers.embeddings._openai_style import OpenAIStyleEmbeddingProvider

logger = get_logger(__name__)


@dataclass(frozen=True)
class EmbeddingPreset:
    base_url: str
    model: str
    dimensions: int
    api_key_env: str | None
    requires_key: bool = True
    #: Only OpenAI's v3 models accept an explicit output width.
    supports_dimensions: bool = True


PRESETS: dict[str, EmbeddingPreset] = {
    "openai": EmbeddingPreset(
        base_url="https://api.openai.com/v1",
        model="text-embedding-3-small",
        dimensions=1536,
        api_key_env="OPENAI_API_KEY",
    ),
    "ollama": EmbeddingPreset(
        base_url="http://localhost:11434/v1",
        model="nomic-embed-text",
        dimensions=768,
        api_key_env=None,
        requires_key=False,
        supports_dimensions=False,
    ),
    "openai_compatible": EmbeddingPreset(
        base_url="", model="", dimensions=1536, api_key_env=None, requires_key=False
    ),
}

#: Chat-only services. Naming them explicitly turns a puzzling 404 on the first
#: write into a clear message at startup.
INFERENCE_ONLY = {
    "groq": "Groq serves chat only and has no embeddings endpoint",
    "fireworks": "Fireworks embedding support varies by account",
    "anthropic": "Anthropic does not offer an embeddings API",
}


class OpenAICompatibleEmbeddingProvider(OpenAIStyleEmbeddingProvider):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int = 1536,
        label: str = "openai",
        settings: Settings | None = None,
        supports_dimensions: bool = True,
    ) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ProviderError("The 'openai' package is required") from exc

        embedding_settings = (settings or Settings()).embedding
        client = AsyncOpenAI(
            base_url=base_url or None,
            api_key=api_key or "not-needed",
            timeout=embedding_settings.timeout_seconds,
            max_retries=0,
        )
        super().__init__(
            client=client,
            model=model,
            label=label,
            dimensions=dimensions,
            settings=embedding_settings,
            send_dimensions=supports_dimensions,
        )


def from_settings(settings: Settings, provider: str) -> OpenAICompatibleEmbeddingProvider:
    if provider in INFERENCE_ONLY:
        raise ProviderError(
            f"{INFERENCE_ONLY[provider]}. Set DREAM_EMBEDDING_PROVIDER to one of "
            "openai, azure_openai or ollama — it does not have to match your LLM provider."
        )

    preset = PRESETS.get(provider)
    if preset is None:
        raise ProviderError(f"Unknown embedding provider: {provider!r}")

    base_url = settings.openai_base_url or preset.base_url
    model = settings.openai_embedding_model or preset.model
    dimensions = settings.openai_embedding_dimensions or preset.dimensions
    api_key = settings.openai_api_key

    if not api_key and preset.api_key_env:
        import os

        api_key = os.environ.get(preset.api_key_env, "")

    if not base_url or not model:
        raise ProviderError(
            f"{provider} needs DREAM_OPENAI_BASE_URL and DREAM_OPENAI_EMBEDDING_MODEL."
        )
    if preset.requires_key and not api_key:
        env_hint = f" or export {preset.api_key_env}" if preset.api_key_env else ""
        raise ProviderError(
            f"{provider} embeddings require an API key. Set DREAM_OPENAI_API_KEY{env_hint}."
        )

    logger.info(
        "embedding_provider_configured", provider=provider, model=model, dimensions=dimensions
    )
    return OpenAICompatibleEmbeddingProvider(
        base_url=base_url,
        api_key=api_key,
        model=model,
        dimensions=dimensions,
        label=provider,
        settings=settings,
        supports_dimensions=preset.supports_dimensions,
    )
