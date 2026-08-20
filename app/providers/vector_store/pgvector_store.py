"""PostgreSQL + pgvector.

    pip install memdream[pgvector]

The pragmatic choice for anyone who already runs Postgres: no new service, and
the tenancy filter is an ordinary indexed WHERE clause rather than a bolt-on.

Schema is one table, with a composite primary key on the scope triple plus the
memory id. That is the isolation guarantee expressed in DDL rather than in
application code — the same memory id under two different tenants is two rows,
and the database enforces it.

Index choice matters here. HNSW is built by default: it costs more to write and
more memory than IVFFlat, but needs no training step and no `lists` parameter
tuned to a row count you do not know yet. For a memory layer, where writes are
steady and the corpus grows continuously, "no retraining" is worth more than
raw build speed.
"""

from __future__ import annotations

import json

from app.core.exceptions import ProviderError
from app.core.logging import get_logger
from app.core.metrics import VECTOR_SEARCH_LATENCY, VECTOR_SEARCHES, metrics
from app.models.scope import MemoryScope
from app.providers.vector_store.base import VectorHit, VectorRecord, VectorStore

logger = get_logger(__name__)


def _literal(vector: list[float]) -> str:
    """pgvector's text input format: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{value:.9g}" for value in vector) + "]"


class PgVectorStore(VectorStore):
    def __init__(
        self,
        *,
        dsn: str,
        table: str = "memdream_vectors",
        dimensions: int = 1536,
        min_pool: int = 1,
        max_pool: int = 10,
    ) -> None:
        try:
            import asyncpg  # noqa: F401
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ProviderError(
                "asyncpg is required. Install with: pip install memdream[pgvector]"
            ) from exc

        self._dsn = dsn
        # Interpolated into DDL, so it must never come from user input. It is
        # operator configuration, and quoted defensively below.
        self._table = table
        self._dimensions = dimensions
        self._min_pool = min_pool
        self._max_pool = max_pool
        self._pool = None

    @property
    def name(self) -> str:
        return "pgvector"

    # --- schema ------------------------------------------------------------- #

    async def _ensure_pool(self):
        if self._pool is not None:
            return self._pool

        import asyncpg

        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=self._min_pool, max_size=self._max_pool
        )
        table = f'"{self._table}"'

        async with self._pool.acquire() as connection:
            await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    client_id  TEXT NOT NULL,
                    bot_id     TEXT NOT NULL,
                    user_id    TEXT NOT NULL,
                    memory_id  TEXT NOT NULL,
                    embedding  vector({self._dimensions}) NOT NULL,
                    metadata   JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    -- Isolation as a constraint, not a convention: the same
                    -- memory id under two tenants is two distinct rows.
                    PRIMARY KEY (client_id, bot_id, user_id, memory_id)
                )
                """
            )
            # Every query filters on the scope triple first.
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {self._table}_scope_idx "
                f"ON {table} (client_id, bot_id, user_id)"
            )
            # HNSW: no training step, no `lists` to tune against a row count that
            # keeps changing. vector_cosine_ops because embeddings arrive
            # L2-normalised and the ranker expects cosine.
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {self._table}_embedding_idx "
                f"ON {table} USING hnsw (embedding vector_cosine_ops)"
            )
        logger.info("pgvector_schema_ready", table=self._table)
        return self._pool

    # --- writes -------------------------------------------------------------- #

    async def upsert(self, scope: MemoryScope, records: list[VectorRecord]) -> None:
        if not records:
            return
        pool = await self._ensure_pool()
        table = f'"{self._table}"'

        rows = [
            (
                scope.client_id,
                scope.bot_id,
                scope.user_id,
                record.memory_id,
                _literal(list(record.vector)),
                json.dumps(record.metadata or {}),
            )
            for record in records
        ]

        async with pool.acquire() as connection:
            await connection.executemany(
                f"""
                INSERT INTO {table}
                    (client_id, bot_id, user_id, memory_id, embedding, metadata, updated_at)
                VALUES ($1, $2, $3, $4, $5::vector, $6::jsonb, now())
                ON CONFLICT (client_id, bot_id, user_id, memory_id) DO UPDATE
                    SET embedding  = EXCLUDED.embedding,
                        metadata   = EXCLUDED.metadata,
                        updated_at = now()
                """,
                rows,
            )

    async def update(self, scope: MemoryScope, records: list[VectorRecord]) -> None:
        await self.upsert(scope, records)

    async def delete(self, scope: MemoryScope, memory_ids: list[str]) -> None:
        if not memory_ids:
            return
        pool = await self._ensure_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                f'DELETE FROM "{self._table}" '
                "WHERE client_id=$1 AND bot_id=$2 AND user_id=$3 AND memory_id = ANY($4::text[])",
                scope.client_id, scope.bot_id, scope.user_id, list(memory_ids),
            )

    # --- reads --------------------------------------------------------------- #

    async def search(
        self,
        scope: MemoryScope,
        query_vector: list[float],
        top_k: int = 10,
        allowed_ids: set[str] | None = None,
    ) -> list[VectorHit]:
        if not query_vector or top_k <= 0:
            return []
        if allowed_ids is not None and not allowed_ids:
            return []

        pool = await self._ensure_pool()
        metrics.increment(VECTOR_SEARCHES, labels={"provider": "pgvector"})

        # `<=>` is cosine distance, so similarity is 1 - distance. Ordering by
        # the operator is what lets the HNSW index serve the query.
        sql = f"""
            SELECT memory_id, metadata, 1 - (embedding <=> $4::vector) AS score
            FROM "{self._table}"
            WHERE client_id = $1 AND bot_id = $2 AND user_id = $3
        """
        params = [scope.client_id, scope.bot_id, scope.user_id, _literal(list(query_vector))]

        if allowed_ids is not None:
            sql += " AND memory_id = ANY($6::text[])"

        sql += " ORDER BY embedding <=> $4::vector LIMIT $5"
        params.append(top_k)
        if allowed_ids is not None:
            params.append(sorted(allowed_ids))

        with metrics.timer(VECTOR_SEARCH_LATENCY, {"provider": "pgvector"}):
            async with pool.acquire() as connection:
                rows = await connection.fetch(sql, *params)

        return [
            VectorHit(
                memory_id=row["memory_id"],
                score=float(row["score"]),
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            )
            for row in rows
        ]

    async def count(self, scope: MemoryScope) -> int:
        pool = await self._ensure_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                f'SELECT count(*) FROM "{self._table}" '
                "WHERE client_id=$1 AND bot_id=$2 AND user_id=$3",
                scope.client_id, scope.bot_id, scope.user_id,
            )
        return int(value or 0)

    async def clear(self, scope: MemoryScope) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                f'DELETE FROM "{self._table}" '
                "WHERE client_id=$1 AND bot_id=$2 AND user_id=$3",
                scope.client_id, scope.bot_id, scope.user_id,
            )

    # --- lifecycle ------------------------------------------------------------ #

    async def health_check(self) -> bool:
        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as connection:
                await connection.fetchval("SELECT 1")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("pgvector_health_check_failed", error=str(exc)[:200])
            return False

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
