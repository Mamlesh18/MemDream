"""ChromaDB vector store.

    pip install memdream[chroma]

The easiest local start: embedded mode persists to a directory, so there is no
server to run. Point it at a host instead for the client/server deployment.

Two things shape this implementation:

* **Chroma's client is synchronous.** Every call is pushed to a thread, or it
  would block the event loop for the duration of a search.
* **Ids are unique per collection, not per tenant.** Using bare memory ids in a
  shared collection would let one tenant's write overwrite another's, so row ids
  are prefixed with the scope digest and stripped again on read.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.exceptions import ProviderError
from app.core.logging import get_logger
from app.core.metrics import VECTOR_SEARCH_LATENCY, VECTOR_SEARCHES, metrics
from app.models.scope import MemoryScope
from app.providers.vector_store._scope_keys import scope_fields, scoped_id, unscoped_id
from app.providers.vector_store.base import VectorHit, VectorRecord, VectorStore

logger = get_logger(__name__)


class ChromaVectorStore(VectorStore):
    def __init__(
        self,
        *,
        path: str | None = None,
        host: str | None = None,
        port: int = 8000,
        collection: str = "memdream",
    ) -> None:
        try:
            import chromadb
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ProviderError(
                "chromadb is required. Install with: pip install memdream[chroma]"
            ) from exc

        if host:
            self._client = chromadb.HttpClient(host=host, port=port)
        else:
            self._client = chromadb.PersistentClient(path=path or "./data/chroma")

        self._collection_name = collection
        self._collection: Any = None

    @property
    def name(self) -> str:
        return "chroma"

    def _get_collection(self) -> Any:
        """Blocking. Always called via `asyncio.to_thread`."""
        if self._collection is None:
            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                # Embeddings arrive L2-normalised and the ranker expects cosine.
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    @staticmethod
    def _where(scope: MemoryScope) -> dict[str, Any]:
        """Chroma needs $and for multi-field equality."""
        return {"$and": [{key: value} for key, value in scope_fields(scope).items()]}

    # --- writes -------------------------------------------------------------- #

    async def upsert(self, scope: MemoryScope, records: list[VectorRecord]) -> None:
        if not records:
            return

        def _work() -> None:
            collection = self._get_collection()
            collection.upsert(
                ids=[scoped_id(scope, record.memory_id) for record in records],
                embeddings=[list(record.vector) for record in records],
                metadatas=[
                    {**scope_fields(scope), "memory_id": record.memory_id, **record.metadata}
                    for record in records
                ],
            )

        await asyncio.to_thread(_work)

    async def update(self, scope: MemoryScope, records: list[VectorRecord]) -> None:
        await self.upsert(scope, records)

    async def delete(self, scope: MemoryScope, memory_ids: list[str]) -> None:
        if not memory_ids:
            return

        def _work() -> None:
            # Delete by scoped id, so this cannot reach another tenant even if a
            # filter were wrong elsewhere.
            self._get_collection().delete(
                ids=[scoped_id(scope, memory_id) for memory_id in memory_ids]
            )

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

        metrics.increment(VECTOR_SEARCHES, labels={"provider": "chroma"})

        def _work() -> Any:
            collection = self._get_collection()
            if collection.count() == 0:
                return None

            where: dict[str, Any] = self._where(scope)
            if allowed_ids is not None:
                where = {
                    "$and": [
                        *where["$and"],
                        {"memory_id": {"$in": sorted(allowed_ids)}},
                    ]
                }
            return collection.query(
                query_embeddings=[list(query_vector)],
                # Chroma caps n_results at the collection size.
                n_results=min(top_k, max(1, collection.count())),
                where=where,
                include=["metadatas", "distances"],
            )

        with metrics.timer(VECTOR_SEARCH_LATENCY, {"provider": "chroma"}):
            result = await asyncio.to_thread(_work)

        if not result or not result.get("ids") or not result["ids"][0]:
            return []

        hits: list[VectorHit] = []
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        for index, row_id in enumerate(result["ids"][0]):
            metadata = metadatas[index] if index < len(metadatas) else {}
            distance = distances[index] if index < len(distances) else 1.0
            hits.append(
                VectorHit(
                    memory_id=metadata.get("memory_id") or unscoped_id(row_id),
                    # Chroma reports cosine *distance*.
                    score=float(1.0 - distance),
                    metadata={
                        k: v
                        for k, v in (metadata or {}).items()
                        if k not in ("client_id", "bot_id", "user_id", "memory_id")
                    },
                )
            )
        return hits[:top_k]

    async def count(self, scope: MemoryScope) -> int:
        def _work() -> int:
            collection = self._get_collection()
            if collection.count() == 0:
                return 0
            found = collection.get(where=self._where(scope), include=[])
            return len(found.get("ids") or [])

        return await asyncio.to_thread(_work)

    async def clear(self, scope: MemoryScope) -> None:
        def _work() -> None:
            collection = self._get_collection()
            if collection.count() == 0:
                return
            collection.delete(where=self._where(scope))

        await asyncio.to_thread(_work)

    # --- lifecycle ------------------------------------------------------------ #

    async def health_check(self) -> bool:
        try:
            await asyncio.to_thread(self._client.heartbeat)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("chroma_health_check_failed", error=str(exc)[:200])
            return False

    async def close(self) -> None:
        self._collection = None
