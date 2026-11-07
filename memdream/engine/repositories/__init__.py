"""Persistence.

`base` holds the contract, `mongo` and `postgres` implement it, and `factory`
picks one. Import the interfaces from here; import a concrete store only if you
are wiring one deliberately.
"""

from memdream.engine.repositories.base import (
    AuditRepository,
    Database,
    JobRepository,
    MemoryRepository,
)
from memdream.engine.repositories.factory import RepositorySet, build_repositories

__all__ = [
    "AuditRepository",
    "Database",
    "JobRepository",
    "MemoryRepository",
    "RepositorySet",
    "build_repositories",
]
