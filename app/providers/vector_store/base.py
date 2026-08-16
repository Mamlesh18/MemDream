"""The vector store contract.

Every method takes a `MemoryScope`. That is the whole tenant-isolation story for
the vector layer: there is no way to *express* a cross-tenant query through this
interface, so isolation does not depend on a caller remembering to pass a filter.
Implementations are expected to partition physically by scope rather than filter
after the fact.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.models.scope import MemoryScope


@dataclass(slots=True)
class VectorRecord:
    """A vector plus the small amount of metadata worth duplicating out of Mongo.

    Only what a post-filter might need lives here; the canonical record always
    stays in Mongo.
    """

    memory_id: str
    vector: list[float]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VectorHit:
    memory_id: str
    #: Cosine similarity in [-1, 1]; in practice [0, 1] for text embeddings.
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


class VectorStore(ABC):
    """Semantic index over memory embeddings."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short provider identifier, used in logs and health output."""

    @abstractmethod
    async def upsert(self, scope: MemoryScope, records: list[VectorRecord]) -> None:
        """Insert or replace vectors within one scope's partition."""

    @abstractmethod
    async def search(
        self,
        scope: MemoryScope,
        query_vector: list[float],
        top_k: int = 10,
        allowed_ids: set[str] | None = None,
    ) -> list[VectorHit]:
        """Nearest neighbours within a scope, best first.

        ``allowed_ids`` narrows the result to a pre-filtered id set — this is how
        metadata filtering (status, memory type, validity window) is applied
        *before* similarity, as required: Mongo decides which memories are
        eligible, the index only ranks them.
        """

    @abstractmethod
    async def delete(self, scope: MemoryScope, memory_ids: list[str]) -> None:
        """Remove vectors.

        Note this deletes an *index entry*, not a memory: Dream never deletes
        memories, but a memory that leaves the searchable set (expired, merged
        away) has no reason to occupy the index.
        """

    @abstractmethod
    async def update(self, scope: MemoryScope, records: list[VectorRecord]) -> None:
        """Replace existing vectors. Equivalent to `upsert` for most backends."""

    @abstractmethod
    async def count(self, scope: MemoryScope) -> int:
        """Number of vectors indexed for a scope."""

    async def clear(self, scope: MemoryScope) -> None:
        """Drop an entire partition. Used by tests and by tenant off-boarding."""
        raise NotImplementedError

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        return None
