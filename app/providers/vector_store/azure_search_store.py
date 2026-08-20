"""Azure AI Search vector store.

    pip install memdream[azure-search]

The natural pairing for a deployment already on Azure OpenAI: same cloud, same
identity story, no extra vendor.

Isolation is an OData filter on the tenancy fields, which are indexed as
filterable. Document keys must match ``[A-Za-z0-9_\\-=]`` — memory ids already
do, but the scope digest is prefixed so one shared index can hold every tenant
without key collisions.

The async client is used throughout, so no thread offloading is needed.
"""

from __future__ import annotations

from typing import Any

from app.core.exceptions import ProviderError
from app.core.logging import get_logger
from app.core.metrics import VECTOR_SEARCH_LATENCY, VECTOR_SEARCHES, metrics
from app.models.scope import MemoryScope
from app.providers.vector_store._scope_keys import scope_fields, scope_key
from app.providers.vector_store.base import VectorHit, VectorRecord, VectorStore

logger = get_logger(__name__)

_OVERFETCH = 4


def _odata_escape(value: str) -> str:
    """Single quotes are escaped by doubling them in OData."""
    return value.replace("'", "''")


class AzureSearchVectorStore(VectorStore):
    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        index_name: str = "memdream",
        dimensions: int = 1536,
    ) -> None:
        try:
            from azure.core.credentials import AzureKeyCredential  # noqa: F401
            from azure.search.documents.aio import SearchClient  # noqa: F401
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ProviderError(
                "azure-search-documents is required. "
                "Install with: pip install memdream[azure-search]"
            ) from exc

        if not endpoint or not api_key:
            raise ProviderError("Azure AI Search requires an endpoint and an API key")

        self._endpoint = endpoint
        self._api_key = api_key
        self._index_name = index_name
        self._dimensions = dimensions
        self._client: Any = None
        self._ready = False

    @property
    def name(self) -> str:
        return "azure_search"

    def _search_client(self) -> Any:
        if self._client is None:
            from azure.core.credentials import AzureKeyCredential
            from azure.search.documents.aio import SearchClient

            self._client = SearchClient(
                endpoint=self._endpoint,
                index_name=self._index_name,
                credential=AzureKeyCredential(self._api_key),
            )
        return self._client

    async def _ensure_index(self) -> None:
        """Create the index if absent. Idempotent, and lazy so the process boots
        even when Search is briefly unavailable."""
        if self._ready:
            return

        from azure.core.credentials import AzureKeyCredential
        from azure.core.exceptions import ResourceNotFoundError
        from azure.search.documents.indexes.aio import SearchIndexClient
        from azure.search.documents.indexes.models import (
            HnswAlgorithmConfiguration,
            SearchField,
            SearchFieldDataType,
            SearchIndex,
            SimpleField,
            VectorSearch,
            VectorSearchProfile,
        )

        index_client = SearchIndexClient(
            endpoint=self._endpoint, credential=AzureKeyCredential(self._api_key)
        )
        try:
            try:
                await index_client.get_index(self._index_name)
                self._ready = True
                return
            except ResourceNotFoundError:
                pass

            index = SearchIndex(
                name=self._index_name,
                fields=[
                    SimpleField(name="id", type=SearchFieldDataType.String, key=True),
                    SimpleField(
                        name="memory_id", type=SearchFieldDataType.String, filterable=True
                    ),
                    SimpleField(
                        name="client_id", type=SearchFieldDataType.String, filterable=True
                    ),
                    SimpleField(name="bot_id", type=SearchFieldDataType.String, filterable=True),
                    SimpleField(name="user_id", type=SearchFieldDataType.String, filterable=True),
                    SearchField(
                        name="embedding",
                        type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                        searchable=True,
                        vector_search_dimensions=self._dimensions,
                        vector_search_profile_name="memdream-profile",
                    ),
                ],
                vector_search=VectorSearch(
                    algorithms=[HnswAlgorithmConfiguration(name="memdream-hnsw")],
                    profiles=[
                        VectorSearchProfile(
                            name="memdream-profile",
                            algorithm_configuration_name="memdream-hnsw",
                        )
                    ],
                ),
            )
            await index_client.create_index(index)
            logger.info("azure_search_index_created", index=self._index_name)
            self._ready = True
        finally:
            await index_client.close()

    @staticmethod
    def _filter(scope: MemoryScope, allowed_ids: set[str] | None = None) -> str:
        clauses = [
            f"{key} eq '{_odata_escape(value)}'" for key, value in scope_fields(scope).items()
        ]
        if allowed_ids is not None:
            ids = ",".join(sorted(_odata_escape(mid) for mid in allowed_ids))
            clauses.append(f"search.in(memory_id, '{ids}', ',')")
        return " and ".join(clauses)

    @staticmethod
    def _document_key(scope: MemoryScope, memory_id: str) -> str:
        # Azure keys allow letters, digits, underscore, dash and equals only.
        return f"{scope_key(scope)}-{memory_id}"

    # --- writes -------------------------------------------------------------- #

    async def upsert(self, scope: MemoryScope, records: list[VectorRecord]) -> None:
        if not records:
            return
        await self._ensure_index()

        await self._search_client().merge_or_upload_documents(
            documents=[
                {
                    "id": self._document_key(scope, record.memory_id),
                    "memory_id": record.memory_id,
                    **scope_fields(scope),
                    "embedding": list(record.vector),
                }
                for record in records
            ]
        )

    async def update(self, scope: MemoryScope, records: list[VectorRecord]) -> None:
        await self.upsert(scope, records)

    async def delete(self, scope: MemoryScope, memory_ids: list[str]) -> None:
        if not memory_ids:
            return
        await self._ensure_index()

        await self._search_client().delete_documents(
            documents=[
                {"id": self._document_key(scope, memory_id)} for memory_id in memory_ids
            ]
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
        if allowed_ids is not None and not allowed_ids:
            return []

        await self._ensure_index()
        metrics.increment(VECTOR_SEARCHES, labels={"provider": "azure_search"})

        from azure.search.documents.models import VectorizedQuery

        vector_query = VectorizedQuery(
            vector=list(query_vector),
            k_nearest_neighbors=top_k if allowed_ids is None else top_k * _OVERFETCH,
            fields="embedding",
        )

        with metrics.timer(VECTOR_SEARCH_LATENCY, {"provider": "azure_search"}):
            results = await self._search_client().search(
                search_text=None,
                vector_queries=[vector_query],
                filter=self._filter(scope, allowed_ids),
                select=["memory_id"],
                top=top_k,
            )

            hits: list[VectorHit] = []
            async for row in results:
                memory_id = row.get("memory_id")
                if not memory_id:
                    continue
                # @search.score for a pure vector query is the cosine similarity.
                hits.append(
                    VectorHit(memory_id=memory_id, score=float(row.get("@search.score", 0.0)))
                )
                if len(hits) >= top_k:
                    break
        return hits

    async def count(self, scope: MemoryScope) -> int:
        await self._ensure_index()
        results = await self._search_client().search(
            search_text="*", filter=self._filter(scope), include_total_count=True, top=0
        )
        return int(await results.get_count() or 0)

    async def clear(self, scope: MemoryScope) -> None:
        await self._ensure_index()
        results = await self._search_client().search(
            search_text="*", filter=self._filter(scope), select=["id"], top=1000
        )
        keys = [{"id": row["id"]} async for row in results]
        if keys:
            await self._search_client().delete_documents(documents=keys)

    # --- lifecycle ------------------------------------------------------------ #

    async def health_check(self) -> bool:
        try:
            await self._ensure_index()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("azure_search_health_check_failed", error=str(exc)[:200])
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
