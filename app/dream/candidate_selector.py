"""Candidate selection for synthesis.

A user with a thousand memories must not turn into a thousand-memory prompt. The
funnel is:

    1000 memories  ->  selector  ->  ~200 candidates  ->  clusters  ->  LLM

Selection is entirely deterministic — importance, recency, type, and evidence
density. Spending a model call to decide what to send to a model call would be
backwards.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.core.config import SynthesisSettings
from app.core.logging import get_logger
from app.models.enums import MemoryStatus, MemoryType
from app.models.memory import Memory, utcnow
from app.models.scope import MemoryScope
from app.repositories.base import MemoryRepository

logger = get_logger(__name__)

#: Types that carry generalisable signal. Behaviours, preferences and habits are
#: what patterns are made of; a one-off event rarely is.
_TYPE_WEIGHTS: dict[str, float] = {
    MemoryType.BEHAVIOR.value: 1.00,
    MemoryType.PREFERENCE.value: 0.95,
    MemoryType.GOAL.value: 0.90,
    MemoryType.SKILL.value: 0.85,
    MemoryType.PROJECT.value: 0.80,
    MemoryType.CONSTRAINT.value: 0.75,
    MemoryType.PROFILE.value: 0.70,
    MemoryType.RELATIONSHIP.value: 0.70,
    MemoryType.FACT.value: 0.60,
    MemoryType.EVENT.value: 0.50,
    MemoryType.OTHER.value: 0.50,
}
_DEFAULT_TYPE_WEIGHT = 0.6

#: Recency matters, but far less than for retrieval: a habit demonstrated over a
#: year is stronger evidence than one demonstrated last week.
_RECENCY_HALF_LIFE_DAYS = 240.0


@dataclass(slots=True)
class SynthesisCandidates:
    memories: list[Memory]
    existing_patterns: list[Memory]
    total_active: int
    #: The floor actually applied, which may be a per-run override rather than
    #: the configured default. Carried so the job can report *which* number it
    #: fell short of — "20 required" is confusing when you asked for 5.
    required: int = 0

    @property
    def has_enough(self) -> bool:
        return bool(self.memories)


class CandidateSelector:
    def __init__(
        self, repository: MemoryRepository, settings: SynthesisSettings | None = None
    ) -> None:
        self._repository = repository
        self._settings = settings or SynthesisSettings()

    def score(self, memory: Memory, now=None) -> float:
        """How likely this memory is to contribute to a pattern."""
        now = now or utcnow()
        type_weight = _TYPE_WEIGHTS.get(memory.memory_type, _DEFAULT_TYPE_WEIGHT)

        reference = memory.updated_at or memory.created_at
        age_days = max((now - reference).total_seconds() / 86400.0, 0.0)
        recency = math.exp(-math.log(2.0) * age_days / _RECENCY_HALF_LIFE_DAYS)

        return (
            0.35 * memory.importance
            + 0.25 * type_weight
            + 0.20 * memory.confidence
            + 0.20 * recency
        )

    async def select(
        self,
        scope: MemoryScope,
        *,
        include_stale_patterns: bool = True,
        min_memories: int | None = None,
    ) -> SynthesisCandidates:
        """Gather the evidence pool and the patterns already built from it.

        ``min_memories`` overrides the configured floor for this run only —
        enough to demonstrate synthesis on a small account without permanently
        lowering the bar for everyone.
        """
        required = self._settings.min_memories if min_memories is None else min_memories
        total_active = await self._repository.count(
            scope,
            statuses=[MemoryStatus.ACTIVE],
            exclude_types=[MemoryType.PATTERN.value],
        )

        if total_active < required:
            logger.info(
                "synthesis_below_minimum",
                active=total_active,
                required=required,
                overridden=min_memories is not None,
            )
            return SynthesisCandidates([], [], total_active, required=required)

        # Pull a generous window, then rank. Ordering by updatedAt first means a
        # very old but very important memory can drop out — acceptable, because
        # a pattern that has had no supporting evidence in years is not one worth
        # spending tokens to rediscover.
        pool = await self._repository.find(
            scope,
            statuses=[MemoryStatus.ACTIVE],
            exclude_types=[MemoryType.PATTERN.value],
            limit=max(self._settings.max_candidates * 3, self._settings.max_candidates),
            sort_field="updatedAt",
        )
        # Memories with no embedding cannot be clustered.
        pool = [memory for memory in pool if memory.embedding]

        now = utcnow()
        pool.sort(key=lambda memory: self.score(memory, now), reverse=True)
        selected = pool[: self._settings.max_candidates]

        patterns = await self._repository.find_patterns(scope, only_active=True)
        if not include_stale_patterns:
            patterns = [pattern for pattern in patterns if not pattern.stale]

        logger.info(
            "synthesis_candidates_selected",
            total_active=total_active,
            pool=len(pool),
            selected=len(selected),
            existing_patterns=len(patterns),
        )
        return SynthesisCandidates(
            memories=selected, existing_patterns=patterns, total_active=total_active
        )
