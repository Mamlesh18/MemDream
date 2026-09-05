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


def _normalise_key(key: Any) -> list[tuple[str, Any]]:
    """Index keys, in a shape the two sides can be compared in.

    `IndexModel.document["key"]` is a SON; `index_information()` returns a list
    of tuples. Order matters and must be preserved — a compound index on
    (a, b) is not the index on (b, a).
    """
    items = key.items() if hasattr(key, "items") else key
    return [(field, direction) for field, direction in items]


async def _sync_indexes(collection: AsyncIOMotorCollection, models: list[IndexModel]) -> None:
    """Create indexes, replacing any of *ours* whose definition has changed.

    MongoDB refuses to create an index whose name is taken by one with a
    different key (`IndexKeySpecsConflict`, code 86), and it is right to: two
    indexes cannot share a name. But that turns a schema change into a startup
    crash on every database that already has data — which is exactly what
    happened when scoped queries moved from a `(clientId, botId, userId)`
    predicate to the single `scopeKey` digest. An index named `scope_status`
    already existed, over different fields, and the service would not boot.

    So a stale index is dropped and rebuilt. That is safe here for a specific
    reason: **only indexes MemDream itself declares by name are touched.** An
    index we no longer match cannot serve any query we issue — it is pure write
    amplification — and anything an operator added under a different name is
    left alone entirely.

    The drop is logged at WARNING, because rebuilding an index on a large
    collection is not free and an operator should see it happen rather than
    discover it in a latency graph.
    """
    try:
        existing = await collection.index_information()
    except Exception as exc:  # noqa: BLE001 - a fresh database has no indexes yet
        logger.debug("index_information_unavailable", error=str(exc)[:200])
        existing = {}

    for model in models:
        document = model.document
        name = document["name"]
        current = existing.get(name)
        if current is None:
            continue

        same_key = _normalise_key(current.get("key", [])) == _normalise_key(document["key"])
        same_unique = bool(current.get("unique", False)) == bool(document.get("unique", False))
        if same_key and same_unique:
            continue

        logger.warning(
            "stale_index_replaced",
            collection=collection.name,
            index=name,
            was=_normalise_key(current.get("key", [])),
            now=_normalise_key(document["key"]),
            detail="index definition changed; dropping and rebuilding",
        )
        await collection.drop_index(name)

    await collection.create_indexes(models)


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
        """Create indexes. Idempotent — safe to run on every boot.

        Indexes whose definition has changed since a previous version are
        replaced rather than allowed to fail the boot; see `_sync_indexes`.

        Note that this reconciles the *schema*, not the *data*. A database
        written before `scopeKey` existed also needs its rows backfilling, or
        every scoped query will match nothing —
        `python -m scripts.migrate_scope_key` does that.
        """
        await _sync_indexes(
            self.memories,
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
                # Consolidation's working set: what has not been useful lately.
                IndexModel(
                    [*_SCOPE_KEYS, ("lastRecalledAt", ASCENDING)], name="scope_recall"
                ),
                # Finding the patterns that rest on a memory that just changed.
                IndexModel([("sourceMemoryIds", ASCENDING)], name="source_memory_ids"),
            ]
        )
        await _sync_indexes(
            self.relations,
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
        await _sync_indexes(
            self.operations,
            [
                IndexModel([*_SCOPE_KEYS, ("timestamp", DESCENDING)], name="scope_timestamp"),
                IndexModel([("memoryId", ASCENDING)], name="memory_id"),
                IndexModel([("jobId", ASCENDING)], name="job_id"),
                IndexModel([("requestId", ASCENDING)], name="request_id"),
            ]
        )
        await _sync_indexes(
            self.jobs,
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
