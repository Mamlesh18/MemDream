"""PostgreSQL implementation of the persistence contract.

    pip install memdream[postgres]
    DREAM_DATABASE_PROVIDER=postgres
    DREAM_POSTGRES_DSN=postgresql://user:pass@localhost:5432/memdream

Chosen over MongoDB when you already run Postgres and would rather not add a
second database — which, for most teams, is the honest reason. Pair it with the
`pgvector` vector store and MemDream needs exactly one piece of infrastructure.

Behaviour is identical to the Mongo implementation, and that claim is tested
rather than asserted: `tests/test_repository_conformance.py` runs the same suite
against both. See `..postgres.database` for what the schema does differently and
why.
"""

from app.repositories.postgres.audit import PostgresAuditRepository
from app.repositories.postgres.database import (
    JOBS,
    MEMORIES,
    OPERATIONS,
    RELATIONS,
    PostgresDatabase,
)
from app.repositories.postgres.jobs import PostgresJobRepository
from app.repositories.postgres.memories import PostgresMemoryRepository

__all__ = [
    "JOBS",
    "MEMORIES",
    "OPERATIONS",
    "RELATIONS",
    "PostgresAuditRepository",
    "PostgresDatabase",
    "PostgresJobRepository",
    "PostgresMemoryRepository",
]
