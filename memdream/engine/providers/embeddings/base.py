"""The embedding provider contract."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod


class EmbeddingProvider(ABC):
    """Turns text into vectors.

    Implementations must return **L2-normalised** vectors. The vector store uses
    inner-product search, which equals cosine similarity only for unit vectors —
    normalising at the provider keeps that invariant in one place instead of
    scattering it through the index code.
    """

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Identifier recorded on each memory, so a later model change is visible."""

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Vector length. Must be stable for the lifetime of an index."""

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Embed a single string."""

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed many strings, preserving order.

        Callers rely on ``result[i]`` corresponding to ``texts[i]``.
        """

    async def close(self) -> None:
        """Release any held connections. Safe to call more than once."""
        return None

    async def health_check(self) -> bool:
        try:
            vector = await self.embed("health check")
            return len(vector) == self.dimensions
        except Exception:
            return False


def l2_normalize(vector: list[float]) -> list[float]:
    """Scale to unit length. A zero vector is returned unchanged rather than
    producing NaNs — an all-zero embedding simply matches nothing."""
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0 or not math.isfinite(norm):
        return vector
    return [component / norm for component in vector]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity that does not assume normalisation.

    Used in the few places where vectors come from mixed sources (e.g. comparing
    a freshly computed proposal against a stored pattern embedding).
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)
