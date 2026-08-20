"""Shared embedding implementation for OpenAI-wire-compatible endpoints.

Same reasoning as the LLM counterpart: Azure OpenAI, OpenAI, Ollama and most
hosted services expose an identical `embeddings` endpoint. Only client
construction and the model name differ, so the batching, retry and
dimension-negotiation logic lives here once.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import EmbeddingSettings
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
from app.providers.llm._openai_style import is_retryable

logger = get_logger(__name__)


class OpenAIStyleEmbeddingProvider(EmbeddingProvider):
    """Embeddings from any OpenAI-compatible endpoint.

    ``dimensions`` is only accepted by OpenAI's v3 embedding models. Rather than
    guessing from a model name — which self-hosted deployments are free to call
    anything — the first request tries with it and permanently falls back if the
    endpoint rejects the parameter.
    """

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        label: str,
        dimensions: int = 1536,
        settings: EmbeddingSettings | None = None,
        send_dimensions: bool = True,
    ) -> None:
        self._client = client
        self._model = model
        self._label = label
        self._settings = settings or EmbeddingSettings()
        self._dimensions = dimensions
        self._send_dimensions = send_dimensions and dimensions > 0

    @property
    def model_name(self) -> str:
        return f"{self._label}:{self._model}"

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
        kwargs: dict[str, Any] = {"model": self._model, "input": inputs}
        if self._send_dimensions:
            kwargs["dimensions"] = self._dimensions

        last_error: Exception | None = None
        for attempt in range(self._settings.max_retries):
            try:
                labels = {"provider": self._label, "operation": current_operation()}
                with metrics.timer(EMBEDDING_LATENCY, labels):
                    response = await self._client.embeddings.create(**kwargs)
                metrics.increment(EMBEDDING_CALLS, labels=labels)
                return response
            except Exception as exc:  # noqa: BLE001 - normalised below
                last_error = exc
                message = str(exc).lower()
                if self._send_dimensions and "dimensions" in message:
                    # This model predates the dimensions parameter, or is a
                    # self-hosted one that never had it.
                    logger.warning(
                        "embedding_dimensions_unsupported",
                        provider=self._label,
                        model=self._model,
                        detail=str(exc)[:200],
                    )
                    self._send_dimensions = False
                    kwargs.pop("dimensions", None)
                    continue
                if not is_retryable(exc) or attempt == self._settings.max_retries - 1:
                    break
                backoff = min(2.0**attempt, 8.0)
                logger.warning(
                    "embedding_retry",
                    provider=self._label,
                    attempt=attempt + 1,
                    backoff_seconds=backoff,
                    error=str(exc)[:200],
                )
                await asyncio.sleep(backoff)

        metrics.increment(EMBEDDING_ERRORS, labels={"provider": self._label})
        raise ProviderError(
            f"{self._label} embedding request failed: {last_error}",
            details={"model": self._model},
        ) from last_error

    def _extract(self, response: Any, expected: int) -> list[list[float]]:
        # The API may return items out of order; `index` is authoritative.
        vectors: list[list[float]] = [[] for _ in range(expected)]
        for item in response.data:
            index = getattr(item, "index", None)
            if index is None or not 0 <= index < expected:
                continue
            vectors[index] = l2_normalize(list(item.embedding))

        usage = getattr(response, "usage", None)
        if usage is not None:
            metrics.increment(
                EMBEDDING_TOKENS,
                float(getattr(usage, "total_tokens", 0) or 0),
                labels={"provider": self._label, "operation": current_operation()},
            )

        if any(not vector for vector in vectors):
            raise ProviderError(f"{self._label} returned fewer embeddings than inputs")

        actual = len(vectors[0])
        if actual != self._dimensions:
            # Trust the endpoint over configuration, but say so loudly: an index
            # built at another width will not accept these vectors.
            logger.warning(
                "embedding_dimension_mismatch",
                provider=self._label,
                configured=self._dimensions,
                actual=actual,
                model=self._model,
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
            results.extend(self._extract(await self._create(chunk), len(chunk)))
        return results

    async def close(self) -> None:
        await self._client.close()
