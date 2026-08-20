"""MongoDB implementation of the persistence contract.

The default store, and the one the framework was designed against: a memory is a
document, and a document is what the reasoner needs — nested metadata, an open
set of memory types, an embedding of whatever width the provider returns, all
without a migration.

Everything here implements `app.repositories.base`. See `..postgres` for the
relational implementation and what changes between them.
"""

from app.repositories.mongo.audit import MongoAuditRepository
from app.repositories.mongo.database import (
    JOBS,
    MEMORIES,
    OPERATIONS,
    RELATIONS,
    MongoDatabase,
    get_database,
    set_database,
)
from app.repositories.mongo.jobs import MongoJobRepository
from app.repositories.mongo.memories import MongoMemoryRepository

__all__ = [
    "JOBS",
    "MEMORIES",
    "OPERATIONS",
    "RELATIONS",
    "MongoAuditRepository",
    "MongoDatabase",
    "MongoJobRepository",
    "MongoMemoryRepository",
    "get_database",
    "set_database",
]
