"""Azure OpenAI embeddings (default: ``text-embedding-3-small``).

Batching, retry and dimension negotiation live in `_openai_style.py`, shared
with the other OpenAI-compatible embedding providers. Azure differs only in
client construction and in addressing models by deployment name.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.exceptions import ProviderError
from app.core.logging import get_logger
from app.providers.embeddings._openai_style import OpenAIStyleEmbeddingProvider

logger = get_logger(__name__)


class AzureOpenAIEmbeddingProvider(OpenAIStyleEmbeddingProvider):
    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        api_version: str,
        deployment: str,
        dimensions: int = 1536,
        settings=None,
    ) -> None:
        try:
            from openai import AsyncAzureOpenAI
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ProviderError(
                "The 'openai' package is required for the Azure OpenAI provider"
            ) from exc

        if not endpoint or not api_key:
            raise ProviderError(
                "AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY must both be set"
            )

        client = AsyncAzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
            timeout=(settings.timeout_seconds if settings else 30.0),
            max_retries=0,
        )
        super().__init__(
            client=client,
            model=deployment,
            label="azure_openai",
            dimensions=dimensions,
            settings=settings,
        )


def from_settings(settings: Settings) -> AzureOpenAIEmbeddingProvider:
    settings.require_azure_openai()
    return AzureOpenAIEmbeddingProvider(
        endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
        deployment=settings.azure_openai_embedding_deployment,
        dimensions=settings.azure_openai_embedding_dimensions,
        settings=settings.embedding,
    )
