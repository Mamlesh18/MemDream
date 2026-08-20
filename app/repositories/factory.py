"""Builds the persistence layer named by ``DREAM_DATABASE_PROVIDER``.

Same shape as the LLM, embedding, vector-store and queue factories: one function,
lazy imports so an unused backend costs nothing, and a clear error for a name
that does not exist.

The three repositories are returned together because they are not independently
selectable. They share a connection and, for the Mongo and Postgres backends,
they share transactions — mixing a Mongo memory store with a Postgres audit log
would give you two half-truths and no way to join them.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError
from app.repositories.base import AuditRepository, Database, JobRepository, MemoryRepository


@dataclass(frozen=True)
class RepositorySet:
    """Everything the container needs from the canonical store."""

    database: Database
    memories: MemoryRepository
    audit: AuditRepository
    jobs: JobRepository


async def build_repositories(
    settings: Settings | None = None, *, database: Database | None = None
) -> RepositorySet:
    """Connect to the configured store and wire its repositories.

    ``database`` overrides the connection, which is how tests inject mongomock
    or a throwaway Postgres schema without touching environment variables.
    """
    settings = settings or get_settings()
    name = settings.database_provider

    if database is not None:
        return _wrap(database)

    if name == "mongodb":
        from app.repositories.mongo import MongoDatabase

        return _wrap(MongoDatabase.connect(settings))

    if name == "postgres":
        from app.repositories.postgres import PostgresDatabase

        return _wrap(await PostgresDatabase.connect(settings))

    raise ConfigurationError(f"Unknown database provider: {name!r}")


def _wrap(database: Database) -> RepositorySet:
    """Pick the repository implementations that match a connection.

    Dispatching on the connection rather than on configuration is what lets an
    injected database work: a test passing mongomock gets the Mongo repositories
    without having to also set `DREAM_DATABASE_PROVIDER`.
    """
    from app.repositories.mongo import MongoDatabase

    if isinstance(database, MongoDatabase):
        from app.repositories.mongo import (
            MongoAuditRepository,
            MongoJobRepository,
            MongoMemoryRepository,
        )

        return RepositorySet(
            database=database,
            memories=MongoMemoryRepository(database),
            audit=MongoAuditRepository(database),
            jobs=MongoJobRepository(database),
        )

    from app.repositories.postgres import PostgresDatabase

    if isinstance(database, PostgresDatabase):
        from app.repositories.postgres import (
            PostgresAuditRepository,
            PostgresJobRepository,
            PostgresMemoryRepository,
        )

        return RepositorySet(
            database=database,
            memories=PostgresMemoryRepository(database),
            audit=PostgresAuditRepository(database),
            jobs=PostgresJobRepository(database),
        )

    raise ConfigurationError(
        f"No repositories are registered for {type(database).__name__}. "
        "Implement app.repositories.base and add a branch here."
    )
