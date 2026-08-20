"""MongoDB connection and index management.

Mongo is the source of truth: every memory, edge, audit row and job lives here,
embeddings included. The FAISS index is a derived artefact that can be thrown
away and rebuilt from these collections.
"""

from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.core.config import Settings, get_settings
from app.core.exceptions import ProviderError
from app.core.logging import get_logger
from app.repositories.base import Database

logger = get_logger(__name__)

MEMORIES = "memories"
RELATIONS = "memory_relations"
OPERATIONS = "dream_operations"
JOBS = "dream_jobs"

#: Prefix on every tenant-scoped index. Declared once so it is obvious that
#: every access path starts from the scope triple rather than ending with it.
#: Every scoped query filters on this single key — see models/scope.py for why
#: one digest beats a predicate over optional dimensions.
_SCOPE_KEYS = [("scopeKey", ASCENDING)]


class MongoDatabase(Database):
    """Owns the Motor client and exposes the four collections."""

    name = "mongodb"

    def __init__(self, client: AsyncIOMotorClient, database_name: str) -> None:
        self._client = client
        self._db: AsyncIOMotorDatabase = client[database_name]

    @classmethod
    def connect(cls, settings: Settings | None = None) -> MongoDatabase:
        settings = settings or get_settings()
        client: AsyncIOMotorClient = AsyncIOMotorClient(
            settings.mongodb_uri,
            # Datetimes come back timezone-aware, matching what the models emit.
            tz_aware=True,
            serverSelectionTimeoutMS=10_000,
            connectTimeoutMS=10_000,
            maxPoolSize=50,
        )
        return cls(client, settings.mongodb_database)

    @property
    def client(self) -> AsyncIOMotorClient:
        return self._client

    @property
    def db(self) -> AsyncIOMotorDatabase:
        return self._db

    @property
    def memories(self) -> AsyncIOMotorCollection:
        return self._db[MEMORIES]

    @property
    def relations(self) -> AsyncIOMotorCollection:
        return self._db[RELATIONS]

    @property
    def operations(self) -> AsyncIOMotorCollection:
        return self._db[OPERATIONS]

    @property
    def jobs(self) -> AsyncIOMotorCollection:
        return self._db[JOBS]

    # --- schema ------------------------------------------------------------- #

    async def ensure_schema(self) -> None:
        """Create indexes. Idempotent — safe to run on every boot."""
        await self.memories.create_indexes(
            [
                # The workhorse: scope + status drives retrieval pre-filtering.
                IndexModel([*_SCOPE_KEYS, ("status", ASCENDING)], name="scope_status"),
                IndexModel(
                    [*_SCOPE_KEYS, ("memoryType", ASCENDING), ("status", ASCENDING)],
                    name="scope_type_status",
                ),
                # Exact-duplicate lookup, which must not need an LLM call.
                IndexModel([*_SCOPE_KEYS, ("contentHash", ASCENDING)], name="scope_content_hash"),
                IndexModel([*_SCOPE_KEYS, ("updatedAt", DESCENDING)], name="scope_updated"),
                IndexModel([*_SCOPE_KEYS, ("createdAt", DESCENDING)], name="scope_created"),
                # Point-in-time queries over the validity window.
                IndexModel(
                    [*_SCOPE_KEYS, ("validFrom", DESCENDING), ("validUntil", DESCENDING)],
                    name="scope_validity",
                ),
                # Finding the patterns that rest on a memory that just changed.
                IndexModel([("sourceMemoryIds", ASCENDING)], name="source_memory_ids"),
            ]
        )
        await self.relations.create_indexes(
            [
                IndexModel(
                    [*_SCOPE_KEYS, ("fromMemoryId", ASCENDING), ("relationType", ASCENDING)],
                    name="scope_from_type",
                ),
                IndexModel(
                    [*_SCOPE_KEYS, ("toMemoryId", ASCENDING), ("relationType", ASCENDING)],
                    name="scope_to_type",
                ),
                # Writing an edge twice must not create a duplicate: relations are
                # re-asserted whenever a Dream operation is retried.
                IndexModel(
                    [
                        ("fromMemoryId", ASCENDING),
                        ("toMemoryId", ASCENDING),
                        ("relationType", ASCENDING),
                    ],
                    name="edge_unique",
                    unique=True,
                ),
            ]
        )
        await self.operations.create_indexes(
            [
                IndexModel([*_SCOPE_KEYS, ("timestamp", DESCENDING)], name="scope_timestamp"),
                IndexModel([("memoryId", ASCENDING)], name="memory_id"),
                IndexModel([("jobId", ASCENDING)], name="job_id"),
                IndexModel([("requestId", ASCENDING)], name="request_id"),
            ]
        )
        await self.jobs.create_indexes(
            [
                IndexModel([*_SCOPE_KEYS, ("createdAt", DESCENDING)], name="scope_created"),
                IndexModel([("status", ASCENDING), ("createdAt", DESCENDING)], name="status_created"),
            ]
        )
        logger.info("mongo_indexes_ready", database=self._db.name)

    async def ensure_indexes(self) -> None:
        """Previous name for `ensure_schema`, kept for existing callers."""
        await self.ensure_schema()

    async def ping(self) -> dict[str, Any]:
        try:
            await self._client.admin.command("ping")
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"MongoDB is unreachable: {exc}") from exc

    async def close(self) -> None:
        self._client.close()

    async def drop_everything(self) -> None:
        """Test helper. Never invoked by the service."""
        for name in (MEMORIES, RELATIONS, OPERATIONS, JOBS):
            await self._db[name].delete_many({})


_database: MongoDatabase | None = None


def get_database(settings: Settings | None = None) -> MongoDatabase:
    """Process-wide database singleton."""
    global _database
    if _database is None:
        _database = MongoDatabase.connect(settings)
    return _database


def set_database(database: MongoDatabase | None) -> None:
    """Inject a database (tests use this to swap in mongomock)."""
    global _database
    _database = database
