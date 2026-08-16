"""In-process vector store.

Same semantics as the FAISS store — per-scope partitions, exact cosine search —
without touching the filesystem. Used by unit tests and by anyone who wants to
run the API with zero infrastructure. Nothing survives a restart.
"""

from __future__ import annotations

import asyncio

import numpy as np

from app.core.metrics import VECTOR_SEARCH_LATENCY, VECTOR_SEARCHES, metrics
from app.models.scope import MemoryScope
from app.providers.vector_store.base import VectorHit, VectorRecord, VectorStore


class InMemoryVectorStore(VectorStore):
    def __init__(self) -> None:
        # partition key -> memory id -> (vector, metadata)
        self._partitions: dict[str, dict[str, tuple[np.ndarray, dict]]] = {}
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "memory"

    def _partition(self, scope: MemoryScope) -> dict[str, tuple[np.ndarray, dict]]:
        return self._partitions.setdefault(scope.partition_key, {})

    @staticmethod
    def _normalize(vector: list[float]) -> np.ndarray:
        array = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(array))
        return array / norm if norm > 0 else array

    async def upsert(self, scope: MemoryScope, records: list[VectorRecord]) -> None:
        if not records:
            return
        async with self._lock:
            partition = self._partition(scope)
            for record in records:
                partition[record.memory_id] = (
                    self._normalize(record.vector),
                    dict(record.metadata),
                )

    async def update(self, scope: MemoryScope, records: list[VectorRecord]) -> None:
        await self.upsert(scope, records)

    async def delete(self, scope: MemoryScope, memory_ids: list[str]) -> None:
        if not memory_ids:
            return
        async with self._lock:
            partition = self._partition(scope)
            for memory_id in memory_ids:
                partition.pop(memory_id, None)

    async def search(
        self,
        scope: MemoryScope,
        query_vector: list[float],
        top_k: int = 10,
        allowed_ids: set[str] | None = None,
    ) -> list[VectorHit]:
        if not query_vector or top_k <= 0:
            return []
        if allowed_ids is not None and not allowed_ids:
            return []

        metrics.increment(VECTOR_SEARCHES, labels={"provider": "memory"})
        with metrics.timer(VECTOR_SEARCH_LATENCY, {"provider": "memory"}):
            async with self._lock:
                partition = self._partition(scope)
                if not partition:
                    return []
                query = self._normalize(query_vector)

                hits: list[VectorHit] = []
                for memory_id, (vector, metadata) in partition.items():
                    if allowed_ids is not None and memory_id not in allowed_ids:
                        continue
                    if vector.shape != query.shape:
                        continue
                    hits.append(
                        VectorHit(
                            memory_id=memory_id,
                            score=float(np.dot(query, vector)),
                            metadata=dict(metadata),
                        )
                    )

            hits.sort(key=lambda hit: hit.score, reverse=True)
            return hits[:top_k]

    async def count(self, scope: MemoryScope) -> int:
        async with self._lock:
            return len(self._partition(scope))

    async def clear(self, scope: MemoryScope) -> None:
        async with self._lock:
            self._partitions.pop(scope.partition_key, None)

    async def destroy_all(self) -> None:
        async with self._lock:
            self._partitions.clear()
