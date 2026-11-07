from __future__ import annotations

from memdream.engine.core.config import Settings, get_settings
from memdream.engine.core.exceptions import ConfigurationError
from memdream.engine.providers.embeddings.base import EmbeddingProvider


def build_embedding_provider(settings: Settings | None = None) -> EmbeddingProvider:
    """Construct the embedding provider named by ``DREAM_EMBEDDING_PROVIDER``."""
    settings = settings or get_settings()
    name = settings.embedding_provider

    if name == "azure_openai":
        from memdream.engine.providers.embeddings.azure_openai import from_settings

        return from_settings(settings)

    if name in ("openai", "ollama", "openai_compatible", "fireworks", "anthropic"):
        from memdream.engine.providers.embeddings.openai_compatible import from_settings

        return from_settings(settings, name)

    if name == "gemini":
        from memdream.engine.providers.embeddings.gemini import from_settings

        return from_settings(settings)

    if name == "fake":
        from memdream.engine.providers.embeddings.fake import FakeEmbeddingProvider

        return FakeEmbeddingProvider()

    raise ConfigurationError(f"Unknown embedding provider: {name!r}")
