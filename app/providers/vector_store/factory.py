"""Vector store selection.

Each backend is an optional extra, imported lazily so that a missing driver
costs nothing until someone actually selects that backend. ``dimensions`` comes
from the live embedding provider rather than configuration, so an index is
always created at the width the model actually returns.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError
from app.providers.vector_store.base import VectorStore


def build_vector_store(
    settings: Settings | None = None, dimensions: int | None = None
) -> VectorStore:
    """Construct the vector store named by ``DREAM_VECTOR_PROVIDER``."""
    settings = settings or get_settings()
    name = settings.vector_provider
    dims = dimensions or settings.azure_openai_embedding_dimensions or 1536

    if name == "faiss":
        from app.providers.vector_store.faiss_store import FaissVectorStore

        return FaissVectorStore(
            path=settings.faiss_path,
            dimensions=dims,
            max_loaded_indexes=settings.faiss_max_loaded_indexes,
        )

    if name == "memory":
        from app.providers.vector_store.memory_store import InMemoryVectorStore

        return InMemoryVectorStore()

    if name == "qdrant":
        from app.providers.vector_store.qdrant_store import QdrantVectorStore

        return QdrantVectorStore(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
            collection=settings.qdrant_collection,
            dimensions=dims,
        )

    if name == "pgvector":
        from app.providers.vector_store.pgvector_store import PgVectorStore

        if not settings.postgres_dsn:
            raise ConfigurationError(
                "pgvector needs a connection string. Set DREAM_POSTGRES_DSN."
            )
        return PgVectorStore(
            dsn=settings.postgres_dsn, table=settings.pgvector_table, dimensions=dims
        )

    if name == "chroma":
        from app.providers.vector_store.chroma_store import ChromaVectorStore

        return ChromaVectorStore(
            path=settings.chroma_path if not settings.chroma_host else None,
            host=settings.chroma_host or None,
            port=settings.chroma_port,
            collection=settings.chroma_collection,
        )

    if name == "pinecone":
        from app.providers.vector_store.pinecone_store import PineconeVectorStore

        if not settings.pinecone_api_key:
            raise ConfigurationError(
                "Pinecone needs an API key. Set DREAM_PINECONE_API_KEY."
            )
        return PineconeVectorStore(
            api_key=settings.pinecone_api_key,
            index_name=settings.pinecone_index,
            dimensions=dims,
            cloud=settings.pinecone_cloud,
            region=settings.pinecone_region,
        )

    if name == "milvus":
        from app.providers.vector_store.milvus_store import MilvusVectorStore

        return MilvusVectorStore(
            uri=settings.milvus_uri,
            token=settings.milvus_token or None,
            collection=settings.milvus_collection,
            dimensions=dims,
        )

    if name == "azure_search":
        from app.providers.vector_store.azure_search_store import AzureSearchVectorStore

        return AzureSearchVectorStore(
            endpoint=settings.azure_search_endpoint,
            api_key=settings.azure_search_api_key,
            index_name=settings.azure_search_index,
            dimensions=dims,
        )

    raise ConfigurationError(f"Unknown vector store provider: {name!r}")
