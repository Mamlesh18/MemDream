from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError
from app.providers.vector_store.base import VectorStore


def build_vector_store(
    settings: Settings | None = None, dimensions: int | None = None
) -> VectorStore:
    """Construct the vector store named by ``DREAM_VECTOR_PROVIDER``.

    ``dimensions`` should come from the live embedding provider rather than
    configuration, so an index is always created at the width the model actually
    returns.
    """
    settings = settings or get_settings()
    name = settings.vector_provider

    if name == "faiss":
        from app.providers.vector_store.faiss_store import FaissVectorStore

        return FaissVectorStore(
            path=settings.faiss_path,
            dimensions=dimensions or settings.azure_openai_embedding_dimensions,
            max_loaded_indexes=settings.faiss_max_loaded_indexes,
        )

    if name == "memory":
        from app.providers.vector_store.memory_store import InMemoryVectorStore

        return InMemoryVectorStore()

    raise ConfigurationError(f"Unknown vector store provider: {name!r}")
