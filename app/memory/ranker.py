"""Hybrid ranking.

Returning the top-k vector matches is not retrieval, it is nearest-neighbour
search. A memory that is slightly less similar but far more important, more
confidently held, and current beats a marginally closer match that is two years
stale and superseded.

    final = w_sem·similarity + w_imp·importance + w_conf·confidence
          + w_rec·recency    + w_status·status_score

All five weights are configuration (`retrieval.weights`), and every component is
returned alongside the total so a ranking can be explained rather than guessed at.
"""

from __future__ import annotations

import math
from datetime import datetime

from app.core.config import RetrievalSettings
from app.models.enums import MemoryStatus
from app.models.memory import Memory, ScoredMemory, utcnow


class HybridRanker:
    def __init__(self, settings: RetrievalSettings | None = None) -> None:
        self._settings = settings or RetrievalSettings()
        weights = self._settings.weights
        total = (
            weights.semantic
            + weights.importance
            + weights.confidence
            + weights.recency
            + weights.status
        )
        # Normalising keeps the final score in [0, 1] whatever weights an
        # operator configures, so `min_score` means the same thing across
        # deployments.
        self._total_weight = total if total > 0 else 1.0

    @property
    def settings(self) -> RetrievalSettings:
        return self._settings

    def recency_score(self, memory: Memory, now: datetime | None = None) -> float:
        """Exponential decay on age, using a configurable half-life.

        Decay is measured from `updatedAt` rather than `createdAt`: a memory
        re-confirmed last week is current knowledge even if first learned years
        ago.
        """
        now = now or utcnow()
        half_life = max(self._settings.recency_half_life_days, 0.001)
        reference = memory.updated_at or memory.created_at
        age_days = max((now - reference).total_seconds() / 86400.0, 0.0)
        return math.exp(-math.log(2.0) * age_days / half_life)

    @staticmethod
    def _status_key(memory: Memory) -> str:
        return (
            memory.status.value
            if isinstance(memory.status, MemoryStatus)
            else str(memory.status)
        )

    def status_score(self, memory: Memory) -> float:
        return self._settings.status_scores.get(self._status_key(memory), 0.0)

    def status_multiplier(self, memory: Memory) -> float:
        return self._settings.status_multipliers.get(self._status_key(memory), 1.0)

    def score(
        self, memory: Memory, similarity: float, now: datetime | None = None
    ) -> ScoredMemory:
        now = now or utcnow()
        weights = self._settings.weights

        # Cosine can be negative; a negative similarity should not be able to
        # pull the total below zero via the weighted sum.
        semantic = max(0.0, min(1.0, similarity))
        recency = self.recency_score(memory, now)
        status = self.status_score(memory)

        total = (
            weights.semantic * semantic
            + weights.importance * memory.importance
            + weights.confidence * memory.confidence
            + weights.recency * recency
            + weights.status * status
        ) / self._total_weight

        # Demote retired memories. See `status_multipliers` for why the additive
        # term above is not sufficient on its own.
        total *= self.status_multiplier(memory)

        return ScoredMemory(
            memory=memory,
            score=total,
            semantic_similarity=semantic,
            importance_score=memory.importance,
            confidence_score=memory.confidence,
            recency_score=recency,
            status_score=status,
        )

    def rank(
        self,
        scored_inputs: list[tuple[Memory, float]],
        *,
        limit: int | None = None,
        min_score: float | None = None,
        now: datetime | None = None,
    ) -> list[ScoredMemory]:
        now = now or utcnow()
        results = [self.score(memory, similarity, now) for memory, similarity in scored_inputs]

        if min_score is not None:
            results = [result for result in results if result.score >= min_score]

        # Ties broken by recency, then by id, so ordering is stable across runs —
        # otherwise an unchanged corpus can return a different order each call.
        results.sort(
            key=lambda item: (item.score, item.recency_score, item.memory.memory_id),
            reverse=True,
        )
        return results[:limit] if limit else results
