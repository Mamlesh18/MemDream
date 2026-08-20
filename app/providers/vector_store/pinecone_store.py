"""Pinecone vector store.

    pip install memdream[pinecone]

Pinecone is the best structural fit of the hosted stores, because it has
**namespaces**: a first-class partition inside an index. One namespace per
tenant means isolation is enforced by the service rather than by a filter this
code has to remember to apply — the same property FAISS gets from one file per
tenant, without the local disk.

Namespaces are free and effectively unlimited, and a query scoped to one cannot
see another. That removes the entire class of bug where a missing filter clause
leaks data across customers.

The client is synchronous, so every call is pushed to a thread.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.exceptions import ProviderError
from app.core.logging import get_logger
from app.core.metrics import VECTOR_SEARCH_LATENCY, VECTOR_SEARCHES, metrics
from app.models.scope import MemoryScope
from app.providers.vector_store._scope_keys import scope_key
from app.providers.vector_store.base import VectorHit, VectorRecord, VectorStore

logger = get_logger(__name__)

#: With a pre-filter, eligible rows may rank below the nearest neighbours.
_OVERFETCH = 4


class PineconeVectorStore(VectorStore):
    def __init__(
        self,
        *,
        api_key: str,
        index_name: str = "memdream",
        dimensions: int = 1536,
        cloud: str = "aws",
        region: str = "us-east-1",
        create_if_missing: bool = True,
    ) -> None:
        try:
            from pinecone import Pinecone
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ProviderError(
                "pinecone is required. Install with: pip install memdream[pinecone]"
            ) from exc

        if not api_key:
            raise ProviderError("Pinecone requires an API key")

        self._pinecone = Pinecone(api_key=api_key)
        self._index_name = index_name
        self._dimensions = dimensions
        self._cloud = cloud
        self._region = region
        self._create_if_missing = create_if_missing
        self._index: Any = None

    @property
    def name(self) -> str:
        return "pinecone"

    def _get_index(self) -> Any:
        """Blocking. Always called via `asyncio.to_thread`."""
        if self._index is not None:
            return self._index

        existing = {spec["name"] for spec in self._pinecone.list_indexes()}
        if self._index_name not in existing:
            if not self._create_if_missing:
                raise ProviderError(f"Pinecone index {self._index_name!r} does not exist")

            from pinecone import ServerlessSpec

            self._pinecone.create_index(
                name=self._index_name,
                dimension=self._dimensions,
                # Embeddings arrive L2-normalised; the ranker expects cosine.
                metric="cosine",
                spec=ServerlessSpec(cloud=self._cloud, region=self._region),
            )
            logger.info("pinecone_index_created", index=self._index_name)

        self._index = self._pinecone.Index(self._index_name)
        return self._index

    # --- writes -------------------------------------------------------------- #

    async def upsert(self, scope: MemoryScope, records: list[VectorRecord]) -> None:
        if not records:
            return

        def _work() -> None:
            self._get_index().upsert(
                vectors=[
                    {
                        "id": record.memory_id,
                        "values": list(record.vector),
                        "metadata": {"memory_id": record.memory_id, **record.metadata},
                    }
                    for record in records
                ],
                # The isolation boundary. Ids need no scope prefix because the
                # namespace already separates tenants.
                namespace=scope_key(scope),
            )

        await asyncio.to_thread(_work)

    async def update(self, scope: MemoryScope, records: list[VectorRecord]) -> None:
        await self.upsert(scope, records)

    async def delete(self, scope: MemoryScope, memory_ids: list[str]) -> None:
        if not memory_ids:
            return

        def _work() -> None:
            try:
                self._get_index().delete(ids=list(memory_ids), namespace=scope_key(scope))
            except Exception as exc:  # noqa: BLE001
                # Deleting an id that was never there is normal on a replayed
                # Dream operation, and some Pinecone tiers 404 rather than no-op.
                if "not found" not in str(exc).lower():
                    raise

        await asyncio.to_thread(_work)

    # --- reads --------------------------------------------------------------- #

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

        metrics.increment(VECTOR_SEARCHES, labels={"provider": "pinecone"})

        def _work() -> Any:
            query: dict[str, Any] = {
                "vector": list(query_vector),
                "top_k": top_k if allowed_ids is None else top_k * _OVERFETCH,
                "namespace": scope_key(scope),
                "include_metadata": True,
            }
            if allowed_ids is not None:
                query["filter"] = {"memory_id": {"$in": sorted(allowed_ids)}}
            return self._get_index().query(**query)

        with metrics.timer(VECTOR_SEARCH_LATENCY, {"provider": "pinecone"}):
            response = await asyncio.to_thread(_work)

        hits: list[VectorHit] = []
        for match in (response or {}).get("matches", []):
            metadata = match.get("metadata") or {}
            hits.append(
                VectorHit(
                    memory_id=metadata.get("memory_id") or match["id"],
                    score=float(match.get("score", 0.0)),
                    metadata={k: v for k, v in metadata.items() if k != "memory_id"},
                )
            )
            if len(hits) >= top_k:
                break
        return hits

    async def count(self, scope: MemoryScope) -> int:
        def _work() -> int:
            stats = self._get_index().describe_index_stats()
            namespaces = stats.get("namespaces") or {}
            entry = namespaces.get(scope_key(scope)) or {}
            return int(entry.get("vector_count", 0))

        return await asyncio.to_thread(_work)

    async def clear(self, scope: MemoryScope) -> None:
        def _work() -> None:
            try:
                # Deleting the namespace is the cleanest possible tenant
                # off-boarding: one call, nothing left behind.
                self._get_index().delete(delete_all=True, namespace=scope_key(scope))
            except Exception as exc:  # noqa: BLE001
                if "not found" not in str(exc).lower():
                    raise

        await asyncio.to_thread(_work)

    # --- lifecycle ------------------------------------------------------------ #

    async def health_check(self) -> bool:
        try:
            await asyncio.to_thread(lambda: self._get_index().describe_index_stats())
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("pinecone_health_check_failed", error=str(exc)[:200])
            return False

    async def close(self) -> None:
        self._index = None
