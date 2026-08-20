"""Milvus / Zilliz vector store.

    pip install memdream[milvus]

Uses `MilvusClient`, which works against a local Milvus, a Zilliz Cloud cluster,
or Milvus Lite (pass a file path as the URI for an embedded, no-server setup).

Isolation is a boolean filter expression on the tenancy fields, backed by scalar
indexes. Milvus also offers a partition key, which would be a closer fit — but it
must be declared at collection creation and cannot be changed afterwards, so a
filter keeps existing deployments upgradable.

Milvus is eventually consistent on write by default. Writes here are flushed so
that a memory is searchable immediately after it is stored — without that, a
supersede could be followed by a search that still returns the retired fact.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.exceptions import ProviderError
from app.core.logging import get_logger
from app.core.metrics import VECTOR_SEARCH_LATENCY, VECTOR_SEARCHES, metrics
from app.models.scope import MemoryScope
from app.providers.vector_store._scope_keys import scope_fields, scoped_id
from app.providers.vector_store.base import VectorHit, VectorRecord, VectorStore

logger = get_logger(__name__)

_OVERFETCH = 4


def _quote(value: str) -> str:
    """Escape a value for a Milvus filter expression."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


class MilvusVectorStore(VectorStore):
    def __init__(
        self,
        *,
        uri: str = "http://localhost:19530",
        token: str | None = None,
        collection: str = "memdream",
        dimensions: int = 1536,
    ) -> None:
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ProviderError(
                "pymilvus is required. Install with: pip install memdream[milvus]"
            ) from exc

        self._client = MilvusClient(uri=uri, token=token or "")
        self._collection = collection
        self._dimensions = dimensions
        self._ready = False

    @property
    def name(self) -> str:
        return "milvus"

    def _ensure_collection(self) -> None:
        """Blocking. Always called via `asyncio.to_thread`."""
        if self._ready:
            return

        if not self._client.has_collection(self._collection):
            from pymilvus import DataType

            schema = self._client.create_schema(auto_id=False, enable_dynamic_field=True)
            schema.add_field("row_id", DataType.VARCHAR, is_primary=True, max_length=512)
            schema.add_field("memory_id", DataType.VARCHAR, max_length=256)
            schema.add_field("client_id", DataType.VARCHAR, max_length=128)
            schema.add_field("bot_id", DataType.VARCHAR, max_length=128)
            schema.add_field("user_id", DataType.VARCHAR, max_length=128)
            schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=self._dimensions)

            index_params = self._client.prepare_index_params()
            index_params.add_index(
                field_name="embedding",
                index_type="HNSW",
                # Embeddings arrive L2-normalised; COSINE matches the ranker.
                metric_type="COSINE",
                params={"M": 16, "efConstruction": 200},
            )
            self._client.create_collection(
                collection_name=self._collection,
                schema=schema,
                index_params=index_params,
            )
            logger.info("milvus_collection_created", collection=self._collection)

        self._client.load_collection(self._collection)
        self._ready = True

    @staticmethod
    def _filter(scope: MemoryScope, allowed_ids: set[str] | None = None) -> str:
        clauses = [f'{key} == "{_quote(value)}"' for key, value in scope_fields(scope).items()]
        if allowed_ids is not None:
            ids = ", ".join(f'"{_quote(mid)}"' for mid in sorted(allowed_ids))
            clauses.append(f"memory_id in [{ids}]")
        return " and ".join(clauses)

    # --- writes -------------------------------------------------------------- #

    async def upsert(self, scope: MemoryScope, records: list[VectorRecord]) -> None:
        if not records:
            return

        def _work() -> None:
            self._ensure_collection()
            self._client.upsert(
                collection_name=self._collection,
                data=[
                    {
                        "row_id": scoped_id(scope, record.memory_id),
                        "memory_id": record.memory_id,
                        **scope_fields(scope),
                        "embedding": list(record.vector),
                    }
                    for record in records
                ],
            )
            # Milvus is eventually consistent by default. A memory must be
            # searchable the instant it is written, or a supersede can be
            # followed by a search that still returns the retired fact.
            self._client.flush(self._collection)

        await asyncio.to_thread(_work)

    async def update(self, scope: MemoryScope, records: list[VectorRecord]) -> None:
        await self.upsert(scope, records)

    async def delete(self, scope: MemoryScope, memory_ids: list[str]) -> None:
        if not memory_ids:
            return

        def _work() -> None:
            self._ensure_collection()
            self._client.delete(
                collection_name=self._collection,
                # By scoped primary key, so this cannot reach another tenant.
                ids=[scoped_id(scope, memory_id) for memory_id in memory_ids],
            )
            self._client.flush(self._collection)

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

        metrics.increment(VECTOR_SEARCHES, labels={"provider": "milvus"})

        def _work() -> Any:
            self._ensure_collection()
            return self._client.search(
                collection_name=self._collection,
                data=[list(query_vector)],
                limit=top_k if allowed_ids is None else top_k * _OVERFETCH,
                filter=self._filter(scope, allowed_ids),
                output_fields=["memory_id"],
            )

        with metrics.timer(VECTOR_SEARCH_LATENCY, {"provider": "milvus"}):
            results = await asyncio.to_thread(_work)

        hits: list[VectorHit] = []
        for row in (results[0] if results else []):
            entity = row.get("entity") or {}
            memory_id = entity.get("memory_id")
            if not memory_id:
                continue
            hits.append(VectorHit(memory_id=memory_id, score=float(row.get("distance", 0.0))))
            if len(hits) >= top_k:
                break
        return hits

    async def count(self, scope: MemoryScope) -> int:
        def _work() -> int:
            self._ensure_collection()
            rows = self._client.query(
                collection_name=self._collection,
                filter=self._filter(scope),
                output_fields=["row_id"],
            )
            return len(rows)

        return await asyncio.to_thread(_work)

    async def clear(self, scope: MemoryScope) -> None:
        def _work() -> None:
            self._ensure_collection()
            self._client.delete(collection_name=self._collection, filter=self._filter(scope))
            self._client.flush(self._collection)

        await asyncio.to_thread(_work)

    # --- lifecycle ------------------------------------------------------------ #

    async def health_check(self) -> bool:
        try:
            await asyncio.to_thread(self._client.list_collections)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("milvus_health_check_failed", error=str(exc)[:200])
            return False

    async def close(self) -> None:
        self._ready = False
