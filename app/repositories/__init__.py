"""Persistence.

`base` holds the contract, `mongo` and `postgres` implement it, and `factory`
picks one. Import the interfaces from here; import a concrete store only if you
are wiring one deliberately.
"""

from app.repositories.base import (
    AuditRepository,
    Database,
    JobRepository,
    MemoryRepository,
)
from app.repositories.factory import RepositorySet, build_repositories

__all__ = [
    "AuditRepository",
    "Database",
    "JobRepository",
    "MemoryRepository",
    "RepositorySet",
    "build_repositories",
]
