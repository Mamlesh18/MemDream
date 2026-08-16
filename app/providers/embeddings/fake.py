"""A deterministic, offline embedding provider.

This is not a toy stub. Random vectors would make every similarity meaningless,
so tests could not exercise the thresholds that drive merge, supersede and
clustering. Instead this is a feature-hashing bag-of-words model: related texts
genuinely score higher than unrelated ones, deterministically and with no
network call.

    "user likes black coffee"  vs  "i prefer black coffee"    -> high
    "user likes black coffee"  vs  "user runs 40km per week"  -> ~0

Good enough to test routing logic; not good enough for production retrieval.
"""

from __future__ import annotations

import hashlib
import math
import re

from app.core.metrics import (
    EMBEDDING_CALLS,
    EMBEDDING_LATENCY,
    EMBEDDING_TOKENS,
    current_operation,
    metrics,
)
from app.providers.embeddings.base import EmbeddingProvider, l2_normalize

_TOKEN = re.compile(r"[a-z0-9]+")

#: Words carrying no topical signal. Dropping them stops every sentence from
#: looking alike simply because they all contain "the" and "is".
_STOPWORDS = frozenset(
    ["a", "an", "the", "and", "or", "but", "if", "then", "than", "that", "this", "these", "those", "of", "in", "on", "at", "to", "for", "with", "without", "from", "by", "as", "is", "are", "was", "were", "be", "been", "being", "am", "do", "does", "did", "doing", "have", "has", "had", "having", "i", "me", "my", "mine", "you", "your", "yours", "he", "him", "his", "she", "her", "hers", "it", "its", "we", "us", "our", "ours", "they", "them", "their", "theirs", "will", "would", "shall", "should", "can", "could", "may", "might", "must", "not", "no", "nor", "so", "very", "just", "about", "into", "over", "under", "again", "more", "most"]
)

#: Deliberately small: fast, and dense enough that unrelated short texts do not
#: accidentally collide onto the same features.
_DEFAULT_DIMENSIONS = 256


class FakeEmbeddingProvider(EmbeddingProvider):
    """Hashed bag-of-words embeddings with unigram + bigram features."""

    def __init__(self, dimensions: int = _DEFAULT_DIMENSIONS, model_name: str = "fake") -> None:
        if dimensions < 8:
            raise ValueError("dimensions must be at least 8")
        self._dimensions = dimensions
        self._model_name = model_name
        #: Exposed so tests can assert the engine batches instead of looping.
        self.call_count = 0
        self.embedded_texts: list[str] = []

    @property
    def model_name(self) -> str:
        return f"fake:{self._model_name}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _bucket(self, feature: str) -> tuple[int, float]:
        """Map a feature to (index, sign).

        The signed hash keeps collisions from systematically inflating
        similarity: two unrelated features landing on the same index cancel out
        about half the time instead of always adding.
        """
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        return value % self._dimensions, 1.0 if (value >> 63) & 1 else -1.0

    def _vector(self, text: str) -> list[float]:
        tokens = [t for t in _TOKEN.findall((text or "").lower()) if t not in _STOPWORDS]
        vector = [0.0] * self._dimensions

        # Unigrams carry topic; bigrams carry a little word order, which is what
        # separates "moved from Chennai to Bangalore" from the reverse.
        features: list[tuple[str, float]] = [(token, 1.0) for token in tokens]
        # Not strict: the offset slice is one shorter by construction, which is
        # exactly what produces consecutive pairs.
        features += [(f"{a}_{b}", 0.5) for a, b in zip(tokens, tokens[1:], strict=False)]

        counts: dict[str, float] = {}
        for feature, weight in features:
            counts[feature] = counts.get(feature, 0.0) + weight

        for feature, count in counts.items():
            index, sign = self._bucket(feature)
            # Sublinear scaling: the tenth mention of a word says little more
            # than the second.
            vector[index] += sign * (1.0 + math.log(count))

        return l2_normalize(vector)

    async def embed(self, text: str) -> list[float]:
        labels = {"provider": "fake", "operation": current_operation()}
        with metrics.timer(EMBEDDING_LATENCY, labels):
            self.call_count += 1
            self.embedded_texts.append(text)
            metrics.increment(EMBEDDING_CALLS, labels=labels)
            # ~4 characters per token is the usual rough ratio; good enough
            # for the offline provider to exercise the reporting path.
            metrics.increment(EMBEDDING_TOKENS, max(1.0, len(text) / 4), labels)
            return self._vector(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        labels = {"provider": "fake", "operation": current_operation()}
        with metrics.timer(EMBEDDING_LATENCY, labels):
            self.call_count += 1
            self.embedded_texts.extend(texts)
            metrics.increment(EMBEDDING_CALLS, labels=labels)
            metrics.increment(
                EMBEDDING_TOKENS, max(1.0, sum(len(t) for t in texts) / 4), labels
            )
            return [self._vector(text) for text in texts]
