"""PostgreSQL connection and schema.

    pip install memdream[postgres]

## Why the schema looks like this

MemDream was designed against a document store, and the temptation when porting
is to keep that shape — one `JSONB` column called `doc` and be done in an
afternoon. That would work and it would be a bad trade: every query becomes a
JSONB path expression, every index an expression index, and the thing Postgres
is actually good at is thrown away.

So the fields that get **filtered, sorted or joined on** are real columns with
real types, and only the genuinely open-ended parts stay `JSONB`:

    namespace   arbitrary partition dimensions a deployment invents
    metadata    arbitrary per-memory annotations
    tokenUsage  provider-shaped, and providers disagree

`memory_type` stays `TEXT` rather than an enum, because the type vocabulary is
deliberately open — a deployment inventing `billing_tier` must not need a
migration. Same reasoning as the Mongo side; here it is a decision with teeth,
since `CREATE TYPE` would have made it a real constraint.

## The embedding column

`DOUBLE PRECISION[]`, and never indexed. That is not an oversight: similarity
search belongs to the vector store, which is configured separately and may well
*be* pgvector pointed at this same database. The copy here exists so the index
stays a derived artefact that can be dropped and rebuilt, which is the same role
it plays in the Mongo schema.

## Isolation

Every table carries `scope_key` and every query filters on it, exactly as the
Mongo implementation filters on `scopeKey`. The individual dimensions are stored
alongside it — never queried, but they make the data legible to an operator with
a `psql` prompt, and support cross-partition reporting.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError, ProviderError
from app.core.logging import get_logger
from app.repositories.base import Database

logger = get_logger(__name__)

MEMORIES = "memdream_memories"
RELATIONS = "memdream_relations"
OPERATIONS = "memdream_operations"
JOBS = "memdream_jobs"

#: Tables in dependency order. Truncation walks it backwards.
ALL_TABLES = (MEMORIES, RELATIONS, OPERATIONS, JOBS)


SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {MEMORIES} (
    memory_id           TEXT PRIMARY KEY,

    -- tenancy: scope_key is what every query filters on; the rest is legibility
    scope_key           TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    agent_id            TEXT,
    client_id           TEXT,
    namespace           JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    phone_number_hash   TEXT,

    content             TEXT NOT NULL,
    memory_type         TEXT NOT NULL DEFAULT 'other',
    status              TEXT NOT NULL DEFAULT 'active',

    importance          DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    confidence          DOUBLE PRECISION NOT NULL DEFAULT 0.8,
    explicit            BOOLEAN NOT NULL DEFAULT TRUE,
    temporal_nature     TEXT NOT NULL DEFAULT 'current',

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- when the fact started being true, as distinct from when we learned it
    valid_from          TIMESTAMPTZ,
    valid_until         TIMESTAMPTZ,

    superseded_by       TEXT,
    merged_into         TEXT,

    source_memory_ids   TEXT[] NOT NULL DEFAULT '{{}}',
    source_message_ids  TEXT[] NOT NULL DEFAULT '{{}}',

    -- a rebuildable copy; similarity search lives in the vector store
    embedding           DOUBLE PRECISION[],
    embedding_model     TEXT,
    embedding_dim       INTEGER,

    content_hash        TEXT NOT NULL DEFAULT '',
    stale               BOOLEAN NOT NULL DEFAULT FALSE,
    last_synthesized_at TIMESTAMPTZ,
    metadata            JSONB NOT NULL DEFAULT '{{}}'::jsonb
);

-- The workhorse: scope + status drives retrieval pre-filtering.
CREATE INDEX IF NOT EXISTS {MEMORIES}_scope_status
    ON {MEMORIES} (scope_key, status);
CREATE INDEX IF NOT EXISTS {MEMORIES}_scope_type_status
    ON {MEMORIES} (scope_key, memory_type, status);
-- Exact-duplicate lookup, which must not need an LLM call.
CREATE INDEX IF NOT EXISTS {MEMORIES}_scope_content_hash
    ON {MEMORIES} (scope_key, content_hash);
CREATE INDEX IF NOT EXISTS {MEMORIES}_scope_updated
    ON {MEMORIES} (scope_key, updated_at DESC);
CREATE INDEX IF NOT EXISTS {MEMORIES}_scope_created
    ON {MEMORIES} (scope_key, created_at DESC);
-- Point-in-time queries over the validity window.
CREATE INDEX IF NOT EXISTS {MEMORIES}_scope_validity
    ON {MEMORIES} (scope_key, valid_from DESC, valid_until DESC);
-- Finding the patterns that rest on a memory that just changed. GIN because
-- the predicate is array containment, which btree cannot answer.
CREATE INDEX IF NOT EXISTS {MEMORIES}_source_memory_ids
    ON {MEMORIES} USING GIN (source_memory_ids);


CREATE TABLE IF NOT EXISTS {RELATIONS} (
    relation_id     TEXT PRIMARY KEY,
    scope_key       TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    agent_id        TEXT,
    client_id       TEXT,
    namespace       JSONB NOT NULL DEFAULT '{{}}'::jsonb,

    from_memory_id  TEXT NOT NULL,
    to_memory_id    TEXT NOT NULL,
    relation_type   TEXT NOT NULL,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    operation_id    TEXT,
    metadata        JSONB NOT NULL DEFAULT '{{}}'::jsonb,

    -- Writing an edge twice must not create a duplicate: relations are
    -- re-asserted whenever a Dream operation is retried. Here the constraint
    -- also powers ON CONFLICT DO NOTHING, so idempotency is the database's
    -- job rather than a read-then-write race in Python.
    UNIQUE (from_memory_id, to_memory_id, relation_type)
);

CREATE INDEX IF NOT EXISTS {RELATIONS}_scope_from_type
    ON {RELATIONS} (scope_key, from_memory_id, relation_type);
CREATE INDEX IF NOT EXISTS {RELATIONS}_scope_to_type
    ON {RELATIONS} (scope_key, to_memory_id, relation_type);


CREATE TABLE IF NOT EXISTS {OPERATIONS} (
    operation_id          TEXT PRIMARY KEY,
    scope_key             TEXT NOT NULL,
    user_id               TEXT NOT NULL,
    agent_id              TEXT,
    client_id             TEXT,
    namespace             JSONB NOT NULL DEFAULT '{{}}'::jsonb,

    action                TEXT NOT NULL,
    memory_id             TEXT,
    target_memory_ids     TEXT[] NOT NULL DEFAULT '{{}}',

    reason                TEXT NOT NULL DEFAULT '',
    input_content         TEXT,
    candidate_memory_ids  TEXT[] NOT NULL DEFAULT '{{}}',

    model                 TEXT,
    provider              TEXT,
    confidence            DOUBLE PRECISION,

    request_id            TEXT,
    job_id                TEXT,
    latency_ms            DOUBLE PRECISION,
    token_usage           JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    validation_notes      TEXT[] NOT NULL DEFAULT '{{}}',

    timestamp             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS {OPERATIONS}_scope_timestamp
    ON {OPERATIONS} (scope_key, timestamp DESC);
CREATE INDEX IF NOT EXISTS {OPERATIONS}_memory_id
    ON {OPERATIONS} (memory_id);
CREATE INDEX IF NOT EXISTS {OPERATIONS}_target_memory_ids
    ON {OPERATIONS} USING GIN (target_memory_ids);
CREATE INDEX IF NOT EXISTS {OPERATIONS}_job_id
    ON {OPERATIONS} (job_id);
CREATE INDEX IF NOT EXISTS {OPERATIONS}_request_id
    ON {OPERATIONS} (request_id);


CREATE TABLE IF NOT EXISTS {JOBS} (
    job_id              TEXT PRIMARY KEY,
    scope_key           TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    agent_id            TEXT,
    client_id           TEXT,
    namespace           JSONB NOT NULL DEFAULT '{{}}'::jsonb,

    status              TEXT NOT NULL DEFAULT 'queued',
    job_type            TEXT NOT NULL DEFAULT 'synthesis',

    memories_analyzed   INTEGER NOT NULL DEFAULT 0,
    clusters_evaluated  INTEGER NOT NULL DEFAULT 0,
    patterns_created    INTEGER NOT NULL DEFAULT 0,
    patterns_updated    INTEGER NOT NULL DEFAULT 0,
    patterns_skipped    INTEGER NOT NULL DEFAULT 0,
    noop_decisions      INTEGER NOT NULL DEFAULT 0,

    error               TEXT,
    created_pattern_ids TEXT[] NOT NULL DEFAULT '{{}}',

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    duration_ms         DOUBLE PRECISION,

    requested_by        TEXT,
    metadata            JSONB NOT NULL DEFAULT '{{}}'::jsonb
);

CREATE INDEX IF NOT EXISTS {JOBS}_scope_created
    ON {JOBS} (scope_key, created_at DESC);
CREATE INDEX IF NOT EXISTS {JOBS}_status_created
    ON {JOBS} (status, created_at DESC);
"""


class PostgresDatabase(Database):
    """Owns the asyncpg pool and the schema."""

    name = "postgres"

    def __init__(self, pool: Any, *, dsn: str = "") -> None:
        self._pool = pool
        self._dsn = dsn

    @classmethod
    async def connect(cls, settings: Settings | None = None) -> PostgresDatabase:
        settings = settings or get_settings()
        if not settings.postgres_dsn:
            raise ConfigurationError(
                "Postgres is selected but DREAM_POSTGRES_DSN is not set. "
                "Example: postgresql://user:pass@localhost:5432/memdream"
            )

        try:
            import asyncpg
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ProviderError(
                "asyncpg is required. Install with: pip install memdream[postgres]"
            ) from exc

        async def _init(connection: Any) -> None:
            # Without this every JSONB column arrives as a string and every
            # write needs a manual json.dumps at the call site. One codec at
            # the connection level keeps dict-in/dict-out true everywhere.
            await connection.set_type_codec(
                "jsonb",
                encoder=json.dumps,
                decoder=json.loads,
                schema="pg_catalog",
            )

        pool = await asyncpg.create_pool(
            settings.postgres_dsn,
            min_size=1,
            max_size=settings.postgres_pool_size,
            command_timeout=30.0,
            init=_init,
        )
        logger.info("postgres_connected", pool_size=settings.postgres_pool_size)
        return cls(pool, dsn=settings.postgres_dsn)

    @property
    def pool(self) -> Any:
        return self._pool

    # --- schema ------------------------------------------------------------- #

    async def ensure_schema(self) -> None:
        """Create tables and indexes. Idempotent — safe on every boot.

        Everything is `IF NOT EXISTS`, so this is a startup step rather than a
        migration tool: a fresh database gets its schema, an existing one gets
        nothing. Schema *changes* between MemDream versions are a migration and
        will be shipped as one.
        """
        async with self._pool.acquire() as connection:
            await connection.execute(SCHEMA_SQL)
        logger.info("postgres_schema_ready", tables=len(ALL_TABLES))

    async def ping(self) -> dict[str, Any]:
        try:
            async with self._pool.acquire() as connection:
                await connection.fetchval("SELECT 1")
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"PostgreSQL is unreachable: {exc}") from exc

    async def close(self) -> None:
        await self._pool.close()

    async def drop_everything(self) -> None:
        """Test helper. Never invoked by the service."""
        async with self._pool.acquire() as connection:
            await connection.execute(
                f"TRUNCATE {', '.join(reversed(ALL_TABLES))} RESTART IDENTITY CASCADE"
            )
