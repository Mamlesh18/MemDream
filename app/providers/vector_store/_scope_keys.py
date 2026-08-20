"""Translating a `MemoryScope` into whatever a vector backend uses to isolate.

FAISS gets physical isolation for free — one index file per tenant. Server-based
stores do not, so each one has to express the same guarantee in its own terms:

    Pinecone   namespace per scope        (hard partition)
    Qdrant     payload filter             (soft, indexed)
    Chroma     metadata `where` clause    (soft)
    pgvector   WHERE on scope columns     (soft, indexed)
    Milvus     boolean filter expression  (soft)
    Azure      OData filter               (soft)

"Soft" means the isolation is a filter the implementation must remember to
apply. That is exactly the kind of thing that is easy to get subtly wrong, which
is why the conformance suite asserts it for every backend rather than trusting
each one.

Two helpers here, and the distinction matters:

* `scope_key` — an opaque, stable digest for namespacing and id prefixing.
* `scope_fields` — the three tenancy values as plain data, for filter clauses.

Both are derived only from the scope, never from anything a model produced.
"""

from __future__ import annotations

import hashlib
import uuid

from app.models.scope import MemoryScope

#: Fixed namespace so `point_uuid` is stable across processes and restarts.
#: Regenerating this would orphan every previously written vector.
_UUID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def scope_key(scope: MemoryScope) -> str:
    """A short, filesystem- and URL-safe digest of the tenancy triple.

    Hashed rather than concatenated for two reasons: tenant ids may contain
    characters that are illegal in a Pinecone namespace or a collection name,
    and a digest keeps customer identifiers out of infrastructure metadata that
    tends to end up in dashboards and logs.
    """
    return hashlib.sha256(scope.partition_key.encode("utf-8")).hexdigest()[:32]


def scope_fields(scope: MemoryScope) -> dict[str, str]:
    """The tenancy triple as metadata, for stores that filter rather than partition."""
    return {
        "client_id": scope.client_id,
        "bot_id": scope.bot_id,
        "user_id": scope.user_id,
    }


def scoped_id(scope: MemoryScope, memory_id: str) -> str:
    """A globally unique row id for stores with one shared keyspace.

    Memory ids are unique *per user*, not globally. In a single shared
    collection, keying on the bare memory id would let one tenant's write
    silently overwrite another's — so the scope digest is part of the key.
    """
    return f"{scope_key(scope)}:{memory_id}"


def unscoped_id(row_id: str) -> str:
    """Recover the memory id from a scoped row id."""
    return row_id.split(":", 1)[1] if ":" in row_id else row_id


def point_uuid(scope: MemoryScope, memory_id: str) -> str:
    """A deterministic UUID for stores that reject arbitrary string ids.

    Qdrant accepts only UUIDs or unsigned integers as point ids, but memory ids
    look like ``mem_a1b2c3``. A UUID5 over the scoped id is stable — the same
    memory always maps to the same point — so an upsert replaces rather than
    duplicating.
    """
    return str(uuid.uuid5(_UUID_NAMESPACE, scoped_id(scope, memory_id)))
