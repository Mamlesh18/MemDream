"""Qdrant vector store.

    pip install memdream[qdrant]

One shared collection, isolated by an indexed payload filter on the tenancy
triple. Collection-per-tenant was the alternative and is worse at scale: Qdrant
holds collection metadata in memory, so thousands of users would mean thousands
of collections.

Two details that are easy to get wrong and that the conformance suite checks:

* **Point ids must be UUIDs or unsigned integers.** Memory ids look like
  ``mem_a1b2c3``, so they are mapped through a deterministic UUID5 — stable, so
  an upsert replaces rather than duplicating.
* **Payload indexes are created on the tenancy fields.** Without them Qdrant
  still filters correctly but scans, and the filter is on the hot path of every
  search.
"""

from __future__ import annotations

import contextlib
from typing import Any

from app.core.exceptions import ProviderError
from app.core.logging import get_logger
from app.core.metrics import VECTOR_SEARCH_LATENCY, VECTOR_SEARCHES, metrics
from app.models.scope import MemoryScope
from app.providers.vector_store._scope_keys import point_uuid, scope_fields
from app.providers.vector_store.base import VectorHit, VectorRecord, VectorStore

logger = get_logger(__name__)

#: With a pre-filter the eligible rows may sit well below the nearest
#: neighbours, so ask for more than top_k and let Qdrant apply the filter.
_OVERFETCH = 4


class QdrantVectorStore(VectorStore):
    def __init__(
        self,
        *,
        url: str = "http://localhost:6333",
        api_key: str | None = None,
        collection: str = "memdream",
        dimensions: int = 1536,
        timeout: float = 30.0,
    ) -> None:
        try:
            from qdrant_client import AsyncQdrantClient
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ProviderError(
                "qdrant-client is required. Install with: pip install memdream[qdrant]"
            ) from exc

        self._client = AsyncQdrantClient(url=url, api_key=api_key, timeout=int(timeout))
        self._collection = collection
        self._dimensions = dimensions
        self._ready = False

    @property
    def name(self) -> str:
        return "qdrant"

    # --- schema ------------------------------------------------------------- #

    async def _ensure_collection(self) -> None:
        """Create the collection and tenancy indexes once, lazily.

        Lazy rather than at construction so the process still boots when Qdrant
        is briefly unavailable — the same reasoning as the Redis pool.
        """
        if self._ready:
            return

        from qdrant_client import models

        if not await self._client.collection_exists(self._collection):
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=models.VectorParams(
                    size=self._dimensions,
                    # Cosine, because the embedding providers return L2-normalised
                    # vectors and the ranker expects cosine similarity.
                    distance=models.Distance.COSINE,
                ),
            )
            logger.info("qdrant_collection_created", collection=self._collection)

        # Indexed, because this filter runs on every single search.
        for field in ("client_id", "bot_id", "user_id", "memory_id"):
            # "already exists" is the normal outcome after first boot.
            with contextlib.suppress(Exception):
                await self._client.create_payload_index(
                    collection_name=self._collection,
                    field_name=field,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )

        self._ready = True

    def _scope_filter(self, scope: MemoryScope, allowed_ids: set[str] | None = None) -> Any:
        from qdrant_client import models

        must = [
            models.FieldCondition(key=key, match=models.MatchValue(value=value))
            for key, value in scope_fields(scope).items()
        ]
        if allowed_ids is not None:
            # Filter by the memory id stored in the payload, not the point id —
            # the caller knows nothing about the UUID mapping.
            must.append(
                models.FieldCondition(
                    key="memory_id", match=models.MatchAny(any=sorted(allowed_ids))
                )
            )
        return models.Filter(must=must)

    # --- writes -------------------------------------------------------------- #

    async def upsert(self, scope: MemoryScope, records: list[VectorRecord]) -> None:
        if not records:
            return
        await self._ensure_collection()

        from qdrant_client import models

        await self._client.upsert(
            collection_name=self._collection,
            points=[
                models.PointStruct(
                    id=point_uuid(scope, record.memory_id),
                    vector=list(record.vector),
                    payload={
                        **scope_fields(scope),
                        "memory_id": record.memory_id,
                        **record.metadata,
                    },
                )
                for record in records
            ],
            wait=True,
        )

    async def update(self, scope: MemoryScope, records: list[VectorRecord]) -> None:
        await self.upsert(scope, records)

    async def delete(self, scope: MemoryScope, memory_ids: list[str]) -> None:
        if not memory_ids:
            return
        await self._ensure_collection()

        from qdrant_client import models

        await self._client.delete(
            collection_name=self._collection,
            # By point id rather than filter: the ids are derived from the scope,
            # so this cannot reach another tenant even if the filter were wrong.
            points_selector=models.PointIdsList(
                points=[point_uuid(scope, memory_id) for memory_id in memory_ids]
            ),
            wait=True,
        )

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
        # An empty allow-list means the pre-filter matched nothing. It must never
        # be read as "no filter".
        if allowed_ids is not None and not allowed_ids:
            return []

        await self._ensure_collection()
        metrics.increment(VECTOR_SEARCHES, labels={"provider": "qdrant"})

        with metrics.timer(VECTOR_SEARCH_LATENCY, {"provider": "qdrant"}):
            response = await self._client.query_points(
                collection_name=self._collection,
                query=list(query_vector),
                query_filter=self._scope_filter(scope, allowed_ids),
                limit=top_k if allowed_ids is None else top_k * _OVERFETCH,
                with_payload=True,
            )

        hits: list[VectorHit] = []
        for point in response.points:
            payload = point.payload or {}
            memory_id = payload.get("memory_id")
            if not memory_id:
                continue
            hits.append(
                VectorHit(
                    memory_id=memory_id,
                    score=float(point.score),
                    metadata={
                        k: v
                        for k, v in payload.items()
                        if k not in ("client_id", "bot_id", "user_id", "memory_id")
                    },
                )
            )
            if len(hits) >= top_k:
                break
        return hits

    async def count(self, scope: MemoryScope) -> int:
        await self._ensure_collection()
        result = await self._client.count(
            collection_name=self._collection,
            count_filter=self._scope_filter(scope),
            exact=True,
        )
        return int(result.count)

    async def clear(self, scope: MemoryScope) -> None:
        await self._ensure_collection()

        from qdrant_client import models

        await self._client.delete(
            collection_name=self._collection,
            points_selector=models.FilterSelector(filter=self._scope_filter(scope)),
            wait=True,
        )

    # --- lifecycle ------------------------------------------------------------ #

    async def health_check(self) -> bool:
        try:
            await self._client.get_collections()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("qdrant_health_check_failed", error=str(exc)[:200])
            return False

    async def close(self) -> None:
        await self._client.close()
