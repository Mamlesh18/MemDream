"""Azure OpenAI chat completions.

All the transport, retry and repair logic lives in `_openai_style.py`, shared
with every other OpenAI-wire-compatible provider. Azure differs in exactly two
ways: the client takes an endpoint plus an api-version, and it addresses models
by *deployment name* rather than model name.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.exceptions import ProviderError
from app.core.logging import get_logger
from app.providers.llm._openai_style import OpenAIStyleLLMProvider

logger = get_logger(__name__)


class AzureOpenAILLMProvider(OpenAIStyleLLMProvider):
    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        api_version: str,
        deployment: str,
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
            timeout=(settings.timeout_seconds if settings else 60.0),
            # Retries are handled upstream so they can be counted and logged.
            max_retries=0,
        )
        # On Azure the "model" argument is the deployment name.
        super().__init__(
            client=client, model=deployment, label="azure_openai", settings=settings
        )


def from_settings(settings: Settings) -> AzureOpenAILLMProvider:
    settings.require_azure_openai()
    return AzureOpenAILLMProvider(
        endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
        deployment=settings.azure_openai_chat_deployment,
        settings=settings.llm,
    )
