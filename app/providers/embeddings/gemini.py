"""Google Gemini embeddings.

    pip install memdream[gemini]

`gemini-embedding-001` supports Matryoshka truncation, so the output width is
selectable — 768 by default here rather than the maximum, because a memory layer
runs a similarity search on every single write and a narrower vector is cheaper
to store and faster to compare, with little accuracy cost at this text length.

One detail worth knowing: truncated Matryoshka vectors are **not** unit length,
so they are re-normalised here. Every vector store in MemDream uses cosine or
inner product and assumes unit vectors, so skipping this would quietly distort
every score.
"""

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
from app.providers.llm._openai_style import is_retryable

logger = get_logger(__name__)

DEFAULT_MODEL = "gemini-embedding-001"
DEFAULT_DIMENSIONS = 768


class GeminiEmbeddingProvider(EmbeddingProvider):
    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        dimensions: int = DEFAULT_DIMENSIONS,
        settings: EmbeddingSettings | None = None,
    ) -> None:
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ProviderError(
                "google-genai is required. Install with: pip install memdream[gemini]"
            ) from exc

        if not api_key:
            raise ProviderError(
                "Gemini requires an API key. Set DREAM_GEMINI_API_KEY or export "
                "GOOGLE_API_KEY."
            )

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._dimensions = dimensions
        self._settings = settings or EmbeddingSettings()

    @property
    def model_name(self) -> str:
        return f"gemini:{self._model}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _prepare(self, text: str) -> str:
        cleaned = (text or "").strip()
        if not cleaned:
            # Empty input is rejected; a space embeds harmlessly and keeps batch
            # indices aligned with the caller's list.
            return " "
        limit = self._settings.max_input_chars
        return cleaned[:limit] if len(cleaned) > limit else cleaned

    async def _embed_many(self, texts: list[str]) -> list[list[float]]:
        from google.genai import types

        config = types.EmbedContentConfig(
            output_dimensionality=self._dimensions,
            # Memories are stored facts, not questions. Gemini tunes the vector
            # space to the stated task, and the wrong one measurably degrades
            # retrieval.
            task_type="SEMANTIC_SIMILARITY",
        )

        last_error: Exception | None = None
        for attempt in range(self._settings.max_retries):
            try:
                labels = {"provider": "gemini", "operation": current_operation()}
                with metrics.timer(EMBEDDING_LATENCY, labels):
                    response = await self._client.aio.models.embed_content(
                        model=self._model, contents=texts, config=config
                    )
                metrics.increment(EMBEDDING_CALLS, labels=labels)

                usage: Any = getattr(response, "metadata", None)
                if usage is not None:
                    metrics.increment(
                        EMBEDDING_TOKENS,
                        float(getattr(usage, "billable_character_count", 0) or 0) / 4,
                        labels,
                    )

                # Matryoshka-truncated vectors are not unit length. Every vector
                # store here assumes they are, so normalise before returning.
                return [l2_normalize(list(item.values)) for item in response.embeddings]

            except Exception as exc:  # noqa: BLE001 - normalised below
                last_error = exc
                if not is_retryable(exc) or attempt == self._settings.max_retries - 1:
                    break
                backoff = min(2.0**attempt, 8.0)
                logger.warning(
                    "embedding_retry", provider="gemini", attempt=attempt + 1,
                    backoff_seconds=backoff, error=str(exc)[:200],
                )
                await asyncio.sleep(backoff)

        metrics.increment(EMBEDDING_ERRORS, labels={"provider": "gemini"})
        raise ProviderError(
            f"Gemini embedding request failed: {last_error}", details={"model": self._model}
        ) from last_error

    async def embed(self, text: str) -> list[float]:
        return (await self._embed_many([self._prepare(text)]))[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        prepared = [self._prepare(text) for text in texts]
        batch_size = max(1, self._settings.batch_size)
        results: list[list[float]] = []
        for start in range(0, len(prepared), batch_size):
            results.extend(await self._embed_many(prepared[start : start + batch_size]))
        return results


def from_settings(settings: Settings) -> GeminiEmbeddingProvider:
    return GeminiEmbeddingProvider(
        api_key=settings.gemini_api_key,
        model=settings.gemini_embedding_model or DEFAULT_MODEL,
        dimensions=settings.gemini_embedding_dimensions or DEFAULT_DIMENSIONS,
        settings=settings.embedding,
    )
