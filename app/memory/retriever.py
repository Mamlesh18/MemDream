"""Scoped retrieval.

Order of operations is the whole point of this module:

    metadata filter (Mongo)  ->  vector similarity (FAISS)  ->  hybrid rank

The filter runs *first*. Mongo decides which memories are eligible — right
tenant, right status, inside the validity window — and only that id set is
allowed to come back from the index. A memory belonging to another client
cannot enter the candidate pool, because it was never in the allow-list and its
vectors live in a different partition.
"""

from __future__ import annotations

from datetime import datetime

from app.core.config import RetrievalSettings, Settings, get_settings
from app.core.logging import get_logger
from app.core.metrics import OP_SEARCH, RETRIEVAL_LATENCY, metrics, operation
from app.memory.ranker import HybridRanker
from app.models.enums import DEFAULT_SEARCH_STATUSES, MemoryStatus, MemoryType
from app.models.memory import Memory, ScoredMemory, utcnow
from app.models.scope import MemoryScope
from app.providers.embeddings.base import EmbeddingProvider
from app.providers.vector_store.base import VectorRecord, VectorStore
from app.repositories.memory_repository import MemoryRepository

logger = get_logger(__name__)


class MemoryRetriever:
    def __init__(
        self,
        repository: MemoryRepository,
        vector_store: VectorStore,
        embeddings: EmbeddingProvider,
        *,
        settings: Settings | None = None,
        ranker: HybridRanker | None = None,
    ) -> None:
        self._repository = repository
        self._vectors = vector_store
        self._embeddings = embeddings
        self._settings = settings or get_settings()
        self._retrieval: RetrievalSettings = self._settings.retrieval
        self._ranker = ranker or HybridRanker(self._retrieval)

    @property
    def ranker(self) -> HybridRanker:
        return self._ranker

    # --- indexing ------------------------------------------------------------ #

    async def index_memory(self, scope: MemoryScope, memory: Memory) -> None:
        """Add or refresh one memory's vector."""
        if not memory.embedding:
            logger.warning("index_skipped_no_embedding", memory_id=memory.memory_id)
            return
        await self._vectors.upsert(
            scope,
            [
                VectorRecord(
                    memory_id=memory.memory_id,
                    vector=memory.embedding,
                    metadata={
                        "status": memory.status.value,
                        "memoryType": memory.memory_type,
                    },
                )
            ],
        )

    async def index_memories(self, scope: MemoryScope, memories: list[Memory]) -> None:
        records = [
            VectorRecord(
                memory_id=memory.memory_id,
                vector=memory.embedding,
                metadata={"status": memory.status.value, "memoryType": memory.memory_type},
            )
            for memory in memories
            if memory.embedding
        ]
        if records:
            await self._vectors.upsert(scope, records)

    async def unindex(self, scope: MemoryScope, memory_ids: list[str]) -> None:
        """Drop vectors for memories that should no longer be searchable.

        Superseded memories stay indexed — historical search needs them. Merged
        ones do not: their content now lives in the canonical memory, and leaving
        them in would return the same fact twice.
        """
        await self._vectors.delete(scope, memory_ids)

    async def clear_index(self, scope: MemoryScope) -> None:
        """Drop a scope's entire vector partition. Erasure path only."""
        await self._vectors.clear(scope)

    # --- search -------------------------------------------------------------- #

    def _statuses_for(
        self,
        *,
        latest_only: bool,
        include_superseded: bool,
        include_merged: bool,
        include_expired: bool,
    ) -> list[MemoryStatus]:
        if latest_only:
            return [MemoryStatus.ACTIVE]
        statuses = [MemoryStatus.ACTIVE]
        if include_superseded:
            statuses.append(MemoryStatus.SUPERSEDED)
        if include_merged:
            statuses.append(MemoryStatus.MERGED)
        if include_expired:
            statuses.append(MemoryStatus.EXPIRED)
        return statuses

    async def search(
        self,
        scope: MemoryScope,
        query: str,
        *,
        limit: int | None = None,
        latest_only: bool = False,
        include_superseded: bool = True,
        include_merged: bool = False,
        include_expired: bool = False,
        memory_types: list[str] | None = None,
        as_of: datetime | None = None,
        min_score: float | None = None,
    ) -> list[ScoredMemory]:
        """Semantic search within one scope, hybrid-ranked."""
        limit = limit or self._retrieval.top_k

        with metrics.timer(RETRIEVAL_LATENCY):
            statuses = self._statuses_for(
                latest_only=latest_only,
                include_superseded=include_superseded,
                include_merged=include_merged,
                include_expired=include_expired,
            )

            # 1. Metadata pre-filter. An empty result here short-circuits: there
            #    is nothing the index could legitimately return.
            allowed = await self._repository.eligible_ids(
                scope, statuses=statuses, memory_types=memory_types, as_of=as_of
            )
            if not allowed:
                return []

            # 2. Semantic ranking, restricted to what survived the filter.
            with operation(OP_SEARCH):
                query_vector = await self._embeddings.embed(query)
            hits = await self._vectors.search(
                scope,
                query_vector,
                top_k=max(limit * self._retrieval.candidate_multiplier, limit),
                allowed_ids=allowed,
            )
            hits = [hit for hit in hits if hit.score >= self._retrieval.similarity_threshold]
            if not hits:
                return []

            # 3. Hybrid re-rank over the canonical records.
            memories = await self._repository.get_many(scope, [hit.memory_id for hit in hits])
            by_id = {memory.memory_id: memory for memory in memories}
            pairs = [
                (by_id[hit.memory_id], hit.score) for hit in hits if hit.memory_id in by_id
            ]
            results = self._ranker.rank(pairs, limit=limit, min_score=min_score)

        logger.debug(
            "memory_search",
            eligible=len(allowed),
            vector_hits=len(hits),
            returned=len(results),
        )
        return results

    # --- candidate selection for Dream ---------------------------------------- #

    async def find_related(
        self,
        scope: MemoryScope,
        *,
        embedding: list[float],
        memory_type: str | None = None,
        similarity_threshold: float = 0.3,
        limit: int = 10,
        exclude_ids: set[str] | None = None,
    ) -> list[tuple[Memory, float]]:
        """Existing memories a new candidate should be reasoned about against.

        Two channels, unioned:

        1. **Vector neighbours** above ``similarity_threshold``.
        2. **Recent active memories of the same type**, regardless of similarity.

        Channel 2 exists because the highest-value Dream cases are the ones
        embeddings are worst at. "User lives in Chennai" and "User moved to
        Bangalore" are a textbook supersede and share almost no vocabulary; a
        similarity-only candidate set would never put them in front of the
        reasoner, and the contradiction would go unnoticed forever. One indexed
        Mongo query closes that gap cheaply.

        Patterns are excluded throughout: a synthesised generalisation must never
        be superseded or merged by an incoming raw fact.
        """
        exclude_ids = exclude_ids or set()
        candidate_config = self._settings.dream.candidates

        allowed = await self._repository.eligible_ids(
            scope,
            statuses=[MemoryStatus.ACTIVE],
            exclude_types=[MemoryType.PATTERN.value],
        )
        allowed -= exclude_ids
        if not allowed:
            return []

        similarities: dict[str, float] = {}

        # Channel 1 — semantic neighbours.
        hits = await self._vectors.search(
            scope, embedding, top_k=max(limit, 20), allowed_ids=allowed
        )
        for hit in hits:
            if hit.score >= similarity_threshold:
                similarities[hit.memory_id] = hit.score

        # Channel 2 — recent memories of the same type.
        if memory_type and candidate_config.same_type_recent > 0:
            recent = await self._repository.find(
                scope,
                statuses=[MemoryStatus.ACTIVE],
                memory_types=[memory_type],
                limit=candidate_config.same_type_recent,
                sort_field="updatedAt",
            )
            for memory in recent:
                if memory.memory_id in exclude_ids or memory.memory_id not in allowed:
                    continue
                # Keep the vector score when there is one; otherwise record the
                # true similarity so the prompt does not overstate the match.
                similarities.setdefault(
                    memory.memory_id,
                    self._cosine(embedding, memory.embedding),
                )

        if not similarities:
            return []

        ordered = sorted(similarities.items(), key=lambda item: item[1], reverse=True)
        ordered = ordered[: min(limit, candidate_config.max_total)]

        memories = await self._repository.get_many(scope, [mid for mid, _ in ordered])
        by_id = {memory.memory_id: memory for memory in memories}
        return [
            (by_id[memory_id], score) for memory_id, score in ordered if memory_id in by_id
        ]

    @staticmethod
    def _cosine(a: list[float], b: list[float] | None) -> float:
        if not b:
            return 0.0
        from app.providers.embeddings.base import cosine_similarity

        return cosine_similarity(a, b)

    # --- convenience ------------------------------------------------------------ #

    async def active_memories(
        self,
        scope: MemoryScope,
        *,
        limit: int = 200,
        exclude_patterns: bool = True,
    ) -> list[Memory]:
        """Active memories, newest first. Used as the synthesis evidence pool."""
        return await self._repository.find(
            scope,
            statuses=[MemoryStatus.ACTIVE],
            exclude_types=[MemoryType.PATTERN.value] if exclude_patterns else None,
            limit=limit,
            sort_field="updatedAt",
        )

    async def default_statuses(self) -> tuple[MemoryStatus, ...]:
        return DEFAULT_SEARCH_STATUSES

    @staticmethod
    def now() -> datetime:
        return utcnow()
