"""Azure OpenAI embeddings (default: ``text-embedding-3-small``)."""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import EmbeddingSettings, Settings
from app.core.exceptions import ProviderError
from app.core.logging import get_logger
from app.core.metrics import (
    EMBEDDING_CALLS,
    EMBEDDING_ERRORS,
    EMBEDDING_LATENCY,
    EMBEDDING_TOKENS,
    current_operation,
    metrics,
)
from app.providers.embeddings.base import EmbeddingProvider, l2_normalize

logger = get_logger(__name__)

#: Errors worth retrying: transient network, rate limiting, 5xx.
_RETRYABLE_MARKERS = (
    "rate limit",
    "429",
    "timeout",
    "timed out",
    "connection",
    "temporarily unavailable",
    "500",
    "502",
    "503",
    "504",
)


def _is_retryable(error: Exception) -> bool:
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    message = str(error).lower()
    return any(marker in message for marker in _RETRYABLE_MARKERS)


class AzureOpenAIEmbeddingProvider(EmbeddingProvider):
    """Embeddings backed by an Azure OpenAI deployment.

    ``dimensions`` is only accepted by the v3 embedding models. Rather than
    guessing from a deployment name (which operators are free to call anything),
    the first request tries with it and permanently falls back if the deployment
    rejects the parameter.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        api_version: str,
        deployment: str,
        dimensions: int = 1536,
        settings: EmbeddingSettings | None = None,
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

        self._settings = settings or EmbeddingSettings()
        self._deployment = deployment
        self._dimensions = dimensions
        self._send_dimensions = dimensions > 0
        self._client = AsyncAzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
            timeout=self._settings.timeout_seconds,
            # Retries are handled here so they can be counted and logged.
            max_retries=0,
        )

    @property
    def model_name(self) -> str:
        return f"azure-openai:{self._deployment}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    # --- internals ---------------------------------------------------------- #

    def _prepare(self, text: str) -> str:
        cleaned = (text or "").strip()
        if not cleaned:
            # The API rejects empty input; a single space embeds harmlessly and
            # keeps batch indices aligned with the caller's list.
            return " "
        limit = self._settings.max_input_chars
        return cleaned[:limit] if len(cleaned) > limit else cleaned

    async def _create(self, inputs: list[str]) -> Any:
        kwargs: dict[str, Any] = {"model": self._deployment, "input": inputs}
        if self._send_dimensions:
            kwargs["dimensions"] = self._dimensions

        last_error: Exception | None = None
        for attempt in range(self._settings.max_retries):
            try:
                labels = {"provider": "azure_openai", "operation": current_operation()}
                with metrics.timer(EMBEDDING_LATENCY, labels):
                    response = await self._client.embeddings.create(**kwargs)
                metrics.increment(EMBEDDING_CALLS, labels=labels)
                return response
            except Exception as exc:  # noqa: BLE001 - normalised into ProviderError below
                last_error = exc
                message = str(exc).lower()
                if self._send_dimensions and "dimensions" in message:
                    # This deployment predates the v3 dimension parameter.
                    logger.warning(
                        "embedding_dimensions_unsupported",
                        deployment=self._deployment,
                        detail=str(exc)[:200],
                    )
                    self._send_dimensions = False
                    kwargs.pop("dimensions", None)
                    continue
                if not _is_retryable(exc) or attempt == self._settings.max_retries - 1:
                    break
                backoff = min(2.0**attempt, 8.0)
                logger.warning(
                    "embedding_retry",
                    attempt=attempt + 1,
                    backoff_seconds=backoff,
                    error=str(exc)[:200],
                )
                await asyncio.sleep(backoff)

        metrics.increment(EMBEDDING_ERRORS, labels={"provider": "azure_openai"})
        raise ProviderError(
            f"Azure OpenAI embedding request failed: {last_error}",
            details={"deployment": self._deployment},
        ) from last_error

    def _extract(self, response: Any, expected: int) -> list[list[float]]:
        # The API may return items out of order; `index` is authoritative.
        vectors: list[list[float]] = [[] for _ in range(expected)]
        for item in response.data:
            index = getattr(item, "index", None)
            vector = l2_normalize(list(item.embedding))
            if index is None or not 0 <= index < expected:
                continue
            vectors[index] = vector

        usage = getattr(response, "usage", None)
        if usage is not None:
            metrics.increment(
                EMBEDDING_TOKENS,
                float(getattr(usage, "total_tokens", 0) or 0),
                labels={"provider": "azure_openai", "operation": current_operation()},
            )

        if any(not vector for vector in vectors):
            raise ProviderError("Azure OpenAI returned fewer embeddings than inputs")

        actual = len(vectors[0])
        if actual != self._dimensions:
            # Trust the deployment over configuration, but say so loudly: an
            # index built at another width will not accept these vectors.
            logger.warning(
                "embedding_dimension_mismatch",
                configured=self._dimensions,
                actual=actual,
                deployment=self._deployment,
            )
            self._dimensions = actual
        return vectors

    # --- API ---------------------------------------------------------------- #

    async def embed(self, text: str) -> list[float]:
        response = await self._create([self._prepare(text)])
        return self._extract(response, 1)[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        prepared = [self._prepare(text) for text in texts]
        batch_size = max(1, self._settings.batch_size)
        results: list[list[float]] = []
        for start in range(0, len(prepared), batch_size):
            chunk = prepared[start : start + batch_size]
            response = await self._create(chunk)
            results.extend(self._extract(response, len(chunk)))
        return results

    async def close(self) -> None:
        await self._client.close()


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
