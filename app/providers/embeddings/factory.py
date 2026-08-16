from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError
from app.providers.embeddings.base import EmbeddingProvider


def build_embedding_provider(settings: Settings | None = None) -> EmbeddingProvider:
    """Construct the embedding provider named by ``DREAM_EMBEDDING_PROVIDER``."""
    settings = settings or get_settings()
    name = settings.embedding_provider

    if name == "azure_openai":
        from app.providers.embeddings.azure_openai import from_settings

        return from_settings(settings)

    if name == "fake":
        from app.providers.embeddings.fake import FakeEmbeddingProvider

        return FakeEmbeddingProvider()

    raise ConfigurationError(f"Unknown embedding provider: {name!r}")
