"""Postgres persistence for memories and the edges between them.

The Mongo implementation of this file is the reference; the two are asserted to
behave identically by `tests/test_repository_conformance.py`. Where they differ
in *how*, it is because the store offers something better:

* Edge idempotency is `ON CONFLICT DO NOTHING` against a unique constraint,
  rather than an upsert-and-hope. The database enforces it.
* `delete_memories` and `delete_scope` run inside a transaction, so a purge
  cannot half-succeed and leave dangling edges.
* Nested metadata updates use `jsonb_set`, which is a true partial update — it
  does not read-modify-write the whole document, so two operations annotating
  different metadata keys cannot clobber each other.

Both rules from `app.repositories.base` hold here: every statement filters on
`scope_key`, and nothing on the Dream path deletes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.exceptions import InvalidStateTransitionError, TenantIsolationError
from app.core.logging import get_logger
from app.models.enums import MemoryStatus, MemoryType, RelationType
from app.models.memory import Memory, MemoryRelation, utcnow
from app.models.scope import MemoryScope
from app.repositories.base import MemoryRepository
from app.repositories.postgres._sql import (
    Filter,
    from_row,
    insert_sql,
    to_row,
    to_snake,
)
from app.repositories.postgres.database import MEMORIES, RELATIONS, PostgresDatabase

logger = get_logger(__name__)

#: Column order for memory writes. Mirrors the schema, and every name is also a
#: `Memory` field name — see `_sql.py` for why that equality is the mapping.
MEMORY_COLUMNS: tuple[str, ...] = (
    "memory_id",
    "scope_key",
    "user_id",
    "agent_id",
    "client_id",
    "namespace",
    "phone_number_hash",
    "content",
    "memory_type",
    "status",
    "importance",
    "confidence",
    "explicit",
    "temporal_nature",
    "created_at",
    "updated_at",
    "valid_from",
    "valid_until",
    "superseded_by",
    "merged_into",
    "source_memory_ids",
    "source_message_ids",
    "embedding",
    "embedding_model",
    "embedding_dim",
    "content_hash",
    "last_recalled_at",
    "recall_count",
    "stale",
    "last_synthesized_at",
    "metadata",
)

RELATION_COLUMNS: tuple[str, ...] = (
    "relation_id",
    "scope_key",
    "user_id",
    "agent_id",
    "client_id",
    "namespace",
    "from_memory_id",
    "to_memory_id",
    "relation_type",
    "created_at",
    "operation_id",
    "metadata",
)

#: Sort keys callers may pass, mapped to columns. An allow-list because the
#: value reaches an ORDER BY, where a parameter placeholder is not permitted —
#: so it is the one place a caller string would otherwise be interpolated.
_SORTABLE = {
    "updatedAt": "updated_at",
    "createdAt": "created_at",
    "importance": "importance",
    "confidence": "confidence",
    "validFrom": "valid_from",
}


class PostgresMemoryRepository(MemoryRepository):
    def __init__(self, database: PostgresDatabase) -> None:
        self._db = database

    @property
    def _pool(self) -> Any:
        return self._db.pool

    # --- helpers ------------------------------------------------------------- #

    def _guard(self, scope: MemoryScope, record: Any) -> dict[str, Any]:
        """Assert a row really belongs to the scope that asked for it.

        The WHERE clause already guarantees this. The check is here because a
        silent cross-tenant read is the single worst failure this system could
        have, and an assertion costs a string comparison.
        """
        row = dict(record)
        if row.get("scope_key") != scope.scope_key:
            logger.error(
                "tenant_isolation_violation",
                expected=scope.partition_key,
                found=row.get("scope_key"),
                memory_id=row.get("memory_id"),
            )
            raise TenantIsolationError("Memory does not belong to the requesting scope")
        return row

    @staticmethod
    def _status_values(
        statuses: list[MemoryStatus] | tuple[MemoryStatus, ...] | None,
    ) -> list[str] | None:
        if not statuses:
            return None
        return [s.value if isinstance(s, MemoryStatus) else str(s) for s in statuses]

    def _build_filter(
        self,
        scope: MemoryScope,
        *,
        statuses: list[MemoryStatus] | tuple[MemoryStatus, ...] | None = None,
        memory_types: list[str] | None = None,
        exclude_types: list[str] | None = None,
        as_of: datetime | None = None,
    ) -> Filter:
        where = Filter().add("scope_key = ?", scope.scope_key)

        status_values = self._status_values(statuses)
        if status_values is not None:
            where.add("status = ANY(?)", status_values)

        if memory_types:
            where.add("memory_type = ANY(?)", [t.strip().lower() for t in memory_types])
        elif exclude_types:
            where.add("NOT (memory_type = ANY(?))", [t.strip().lower() for t in exclude_types])

        if as_of is not None:
            # An absent bound means "unknown", not "excluded": no valid_from
            # reads as always-having-been-true, no valid_until as still true.
            where.add("(valid_from IS NULL OR valid_from <= ?)", as_of)
            where.add("(valid_until IS NULL OR valid_until > ?)", as_of)

        return where

    # --- writes -------------------------------------------------------------- #

    async def insert(self, memory: Memory) -> Memory:
        await self._pool.execute(
            insert_sql(MEMORIES, MEMORY_COLUMNS), *to_row(memory, MEMORY_COLUMNS)
        )
        return memory

    async def insert_many(self, memories: list[Memory]) -> list[Memory]:
        if not memories:
            return []
        # executemany pipelines the whole batch in one round trip.
        await self._pool.executemany(
            insert_sql(MEMORIES, MEMORY_COLUMNS),
            [to_row(memory, MEMORY_COLUMNS) for memory in memories],
        )
        return memories

    async def update_fields(
        self, scope: MemoryScope, memory_id: str, fields: dict[str, Any]
    ) -> Memory | None:
        """Patch a memory in place, always bumping ``updated_at``.

        Keys arrive in the wire spelling (`embeddingModel`), and a dotted key
        annotates JSONB rather than replacing it — `metadata.claimedBy` sets one
        key and leaves the rest of the object alone.
        """
        assignments: list[str] = []
        where = Filter()

        for key, value in {**fields, "updatedAt": utcnow()}.items():
            column, _, nested = key.partition(".")
            column = to_snake(column)
            if nested:
                # jsonb_set is a genuine partial update: two operations writing
                # different metadata keys cannot clobber each other, which a
                # read-modify-write of the whole object would allow.
                placeholder = where.bind(value)
                path = where.bind([nested])
                assignments.append(
                    f"{column} = jsonb_set(COALESCE({column}, '{{}}'::jsonb), "
                    f"{path}::text[], to_jsonb({placeholder}), true)"
                )
            else:
                assignments.append(f"{column} = {where.bind(value)}")

        where.add("memory_id = ?", memory_id).add("scope_key = ?", scope.scope_key)
        record = await self._pool.fetchrow(
            f"UPDATE {MEMORIES} SET {', '.join(assignments)} "
            f"WHERE {where.sql} RETURNING *",
            *where.params,
        )
        if record is None:
            return None
        return from_row(self._guard(scope, record), Memory)

    async def replace(self, scope: MemoryScope, memory: Memory) -> Memory:
        memory.touch()
        where = Filter()
        assignments = ", ".join(
            f"{column} = {where.bind(value)}"
            for column, value in zip(
                MEMORY_COLUMNS, to_row(memory, MEMORY_COLUMNS), strict=True
            )
            if column != "memory_id"
        )
        where.add("memory_id = ?", memory.memory_id).add("scope_key = ?", scope.scope_key)
        await self._pool.execute(
            f"UPDATE {MEMORIES} SET {assignments} WHERE {where.sql}", *where.params
        )
        return memory

    # --- lifecycle transitions ------------------------------------------------ #

    async def mark_superseded(
        self,
        scope: MemoryScope,
        memory_ids: list[str],
        *,
        superseded_by: str,
        valid_until: datetime | None = None,
        operation_id: str | None = None,
    ) -> list[str]:
        if not memory_ids:
            return []

        fields: dict[str, Any] = {
            "status": MemoryStatus.SUPERSEDED.value,
            "supersededBy": superseded_by,
        }
        if valid_until is not None:
            fields["validUntil"] = valid_until
        if operation_id:
            fields["metadata.supersededByOperation"] = operation_id

        modified = await self._transition(scope, memory_ids, fields)
        if modified != len(memory_ids):
            logger.info(
                "supersede_partial",
                requested=len(memory_ids),
                modified=modified,
                detail="targets already in a terminal state were skipped",
            )
        return await self._transitioned_ids(scope, memory_ids, MemoryStatus.SUPERSEDED)

    async def mark_merged(
        self,
        scope: MemoryScope,
        memory_ids: list[str],
        *,
        merged_into: str,
        operation_id: str | None = None,
    ) -> list[str]:
        if not memory_ids:
            return []
        if merged_into in memory_ids:
            raise InvalidStateTransitionError("A memory cannot be merged into itself")

        fields: dict[str, Any] = {
            "status": MemoryStatus.MERGED.value,
            "mergedInto": merged_into,
        }
        if operation_id:
            fields["metadata.mergedByOperation"] = operation_id

        await self._transition(scope, memory_ids, fields)
        return await self._transitioned_ids(scope, memory_ids, MemoryStatus.MERGED)

    async def mark_expired(
        self, scope: MemoryScope, memory_ids: list[str], *, valid_until: datetime | None = None
    ) -> list[str]:
        if not memory_ids:
            return []
        fields: dict[str, Any] = {"status": MemoryStatus.EXPIRED.value}
        if valid_until is not None:
            fields["validUntil"] = valid_until

        await self._transition(scope, memory_ids, fields)
        return await self._transitioned_ids(scope, memory_ids, MemoryStatus.EXPIRED)

    async def _transition(
        self, scope: MemoryScope, memory_ids: list[str], fields: dict[str, Any]
    ) -> int:
        """Move *active* rows only, and report how many actually moved.

        The `status = 'active'` predicate is what makes a replayed Dream
        operation idempotent: a memory already superseded or merged is history,
        and rewriting it would corrupt the chain.
        """
        assignments: list[str] = []
        where = Filter()

        for key, value in {**fields, "updatedAt": utcnow()}.items():
            column, _, nested = key.partition(".")
            column = to_snake(column)
            if nested:
                placeholder = where.bind(value)
                path = where.bind([nested])
                assignments.append(
                    f"{column} = jsonb_set(COALESCE({column}, '{{}}'::jsonb), "
                    f"{path}::text[], to_jsonb({placeholder}), true)"
                )
            else:
                assignments.append(f"{column} = {where.bind(value)}")

        where.add("memory_id = ANY(?)", memory_ids)
        where.add("scope_key = ?", scope.scope_key)
        where.add("status = ?", MemoryStatus.ACTIVE.value)

        status = await self._pool.execute(
            f"UPDATE {MEMORIES} SET {', '.join(assignments)} WHERE {where.sql}", *where.params
        )
        # asyncpg returns the command tag, e.g. "UPDATE 3".
        return int(status.rsplit(" ", 1)[-1]) if status else 0

    async def _transitioned_ids(
        self, scope: MemoryScope, memory_ids: list[str], status: MemoryStatus
    ) -> list[str]:
        """Ids that actually reached ``status`` — the honest result of a transition."""
        records = await self._pool.fetch(
            f"SELECT memory_id FROM {MEMORIES} "
            "WHERE memory_id = ANY($1) AND scope_key = $2 AND status = $3",
            memory_ids,
            scope.scope_key,
            status.value,
        )
        return [record["memory_id"] for record in records]

    async def record_recall(
        self, scope: MemoryScope, memory_ids: list[str], *, at: datetime | None = None
    ) -> int:
        if not memory_ids:
            return 0
        # No `updated_at` here, on purpose — see the contract in base.py.
        status = await self._pool.execute(
            f"UPDATE {MEMORIES} SET last_recalled_at = $1, recall_count = recall_count + 1 "
            "WHERE memory_id = ANY($2) AND scope_key = $3",
            at or utcnow(),
            memory_ids,
            scope.scope_key,
        )
        return int(status.rsplit(" ", 1)[-1]) if status else 0

    async def mark_stale(
        self, scope: MemoryScope, memory_ids: list[str], stale: bool = True
    ) -> int:
        if not memory_ids:
            return 0
        status = await self._pool.execute(
            f"UPDATE {MEMORIES} SET stale = $1, updated_at = $2 "
            "WHERE memory_id = ANY($3) AND scope_key = $4",
            stale,
            utcnow(),
            memory_ids,
            scope.scope_key,
        )
        return int(status.rsplit(" ", 1)[-1]) if status else 0

    # --- reads ---------------------------------------------------------------- #

    async def get(self, scope: MemoryScope, memory_id: str) -> Memory | None:
        record = await self._pool.fetchrow(
            f"SELECT * FROM {MEMORIES} WHERE memory_id = $1 AND scope_key = $2",
            memory_id,
            scope.scope_key,
        )
        if record is None:
            return None
        return from_row(self._guard(scope, record), Memory)

    async def get_many(self, scope: MemoryScope, memory_ids: list[str]) -> list[Memory]:
        if not memory_ids:
            return []
        records = await self._pool.fetch(
            f"SELECT * FROM {MEMORIES} WHERE memory_id = ANY($1) AND scope_key = $2",
            memory_ids,
            scope.scope_key,
        )
        # Reordered in Python rather than with a CASE expression: callers pass
        # ids in relevance order from the vector index and expect that order
        # back, and the set is at most a page.
        found = {
            record["memory_id"]: from_row(self._guard(scope, record), Memory)
            for record in records
        }
        return [found[mid] for mid in memory_ids if mid in found]

    async def find(
        self,
        scope: MemoryScope,
        *,
        statuses: list[MemoryStatus] | tuple[MemoryStatus, ...] | None = None,
        memory_types: list[str] | None = None,
        exclude_types: list[str] | None = None,
        as_of: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        sort_field: str = "updatedAt",
        include_embedding: bool = True,
    ) -> list[Memory]:
        where = self._build_filter(
            scope,
            statuses=statuses,
            memory_types=memory_types,
            exclude_types=exclude_types,
            as_of=as_of,
        )
        # NULL as embedding rather than dropping the column: `Memory` requires
        # every field, and a projection that omits one would fail validation.
        columns = "*" if include_embedding else (
            ", ".join(
                "NULL::double precision[] AS embedding" if c == "embedding" else c
                for c in MEMORY_COLUMNS
            )
        )
        order_by = _SORTABLE.get(sort_field, "updated_at")
        sql = (
            f"SELECT {columns} FROM {MEMORIES} WHERE {where.sql} "
            f"ORDER BY {order_by} DESC "
            f"LIMIT {where.bind(max(1, limit))} OFFSET {where.bind(max(0, offset))}"
        )
        records = await self._pool.fetch(sql, *where.params)
        return [from_row(self._guard(scope, record), Memory) for record in records]

    async def eligible_ids(
        self,
        scope: MemoryScope,
        *,
        statuses: list[MemoryStatus] | tuple[MemoryStatus, ...] | None = None,
        memory_types: list[str] | None = None,
        exclude_types: list[str] | None = None,
        as_of: datetime | None = None,
    ) -> set[str]:
        where = self._build_filter(
            scope,
            statuses=statuses,
            memory_types=memory_types,
            exclude_types=exclude_types,
            as_of=as_of,
        )
        records = await self._pool.fetch(
            f"SELECT memory_id FROM {MEMORIES} WHERE {where.sql}", *where.params
        )
        return {record["memory_id"] for record in records}

    async def count(
        self,
        scope: MemoryScope,
        statuses: list[MemoryStatus] | tuple[MemoryStatus, ...] | None = None,
        memory_types: list[str] | None = None,
        exclude_types: list[str] | None = None,
    ) -> int:
        where = self._build_filter(
            scope, statuses=statuses, memory_types=memory_types, exclude_types=exclude_types
        )
        return await self._pool.fetchval(
            f"SELECT count(*) FROM {MEMORIES} WHERE {where.sql}", *where.params
        )

    async def count_since(self, scope: MemoryScope, since: datetime | None = None) -> int:
        where = (
            Filter()
            .add("scope_key = ?", scope.scope_key)
            .add("status = ?", MemoryStatus.ACTIVE.value)
            .add("memory_type <> ?", MemoryType.PATTERN.value)
        )
        if since is not None:
            where.add("created_at > ?", since)
        return await self._pool.fetchval(
            f"SELECT count(*) FROM {MEMORIES} WHERE {where.sql}", *where.params
        )

    async def find_by_content_hash(
        self,
        scope: MemoryScope,
        content_hash: str,
        statuses: tuple[MemoryStatus, ...] = (MemoryStatus.ACTIVE,),
    ) -> Memory | None:
        record = await self._pool.fetchrow(
            f"SELECT * FROM {MEMORIES} "
            "WHERE scope_key = $1 AND content_hash = $2 AND status = ANY($3) "
            "LIMIT 1",
            scope.scope_key,
            content_hash,
            self._status_values(list(statuses)) or [],
        )
        if record is None:
            return None
        return from_row(self._guard(scope, record), Memory)

    async def find_patterns(
        self, scope: MemoryScope, *, only_active: bool = True, limit: int = 500
    ) -> list[Memory]:
        return await self.find(
            scope,
            statuses=[MemoryStatus.ACTIVE] if only_active else None,
            memory_types=[MemoryType.PATTERN.value],
            limit=limit,
        )

    async def find_patterns_referencing(
        self, scope: MemoryScope, source_memory_ids: list[str]
    ) -> list[Memory]:
        if not source_memory_ids:
            return []
        # `&&` is array overlap, answered by the GIN index on source_memory_ids.
        records = await self._pool.fetch(
            f"SELECT * FROM {MEMORIES} "
            "WHERE scope_key = $1 AND memory_type = $2 AND source_memory_ids && $3",
            scope.scope_key,
            MemoryType.PATTERN.value,
            source_memory_ids,
        )
        return [from_row(self._guard(scope, record), Memory) for record in records]

    async def iter_all(self, scope: MemoryScope, batch_size: int = 500) -> list[Memory]:
        records = await self._pool.fetch(
            f"SELECT * FROM {MEMORIES} WHERE scope_key = $1", scope.scope_key
        )
        return [from_row(self._guard(scope, record), Memory) for record in records]

    async def distinct_scopes(self, limit: int = 10000) -> list[dict[str, str]]:
        records = await self._pool.fetch(
            "SELECT scope_key, user_id, agent_id, client_id, namespace, count(*) AS count "
            f"FROM {MEMORIES} "
            "GROUP BY scope_key, user_id, agent_id, client_id, namespace "
            "LIMIT $1",
            limit,
        )
        return [
            {
                "scopeKey": record["scope_key"],
                "userId": record["user_id"],
                "agentId": record["agent_id"],
                "clientId": record["client_id"],
                "namespace": record["namespace"] or {},
                "count": record["count"],
                # Previous name for agentId, kept so existing callers work.
                "botId": record["agent_id"],
            }
            for record in records
        ]

    # --- relations -------------------------------------------------------------- #

    async def add_relation(
        self,
        scope: MemoryScope,
        from_memory_id: str,
        to_memory_id: str,
        relation_type: RelationType,
        *,
        operation_id: str | None = None,
        bidirectional: bool = True,
    ) -> None:
        if from_memory_id == to_memory_id:
            return

        edges = [(from_memory_id, to_memory_id, relation_type)]
        if bidirectional:
            edges.append((to_memory_id, from_memory_id, relation_type.inverse))

        rows = [
            to_row(
                MemoryRelation(
                    **scope.as_model_fields(),
                    from_memory_id=source,
                    to_memory_id=target,
                    relation_type=edge_type,
                    operation_id=operation_id,
                ),
                RELATION_COLUMNS,
            )
            for source, target, edge_type in edges
        ]
        # The unique constraint makes idempotency the database's job. Re-running
        # a Dream operation converges on the same graph instead of racing a
        # read-then-write in Python.
        await self._pool.executemany(
            insert_sql(RELATIONS, RELATION_COLUMNS)
            + " ON CONFLICT (from_memory_id, to_memory_id, relation_type) DO NOTHING",
            rows,
        )

    async def add_relations(
        self,
        scope: MemoryScope,
        from_memory_id: str,
        to_memory_ids: list[str],
        relation_type: RelationType,
        *,
        operation_id: str | None = None,
    ) -> None:
        for target in to_memory_ids:
            await self.add_relation(
                scope, from_memory_id, target, relation_type, operation_id=operation_id
            )

    async def get_relations(
        self,
        scope: MemoryScope,
        memory_id: str,
        relation_types: list[RelationType] | None = None,
    ) -> list[MemoryRelation]:
        return await self._relations("from_memory_id", scope, memory_id, relation_types)

    async def get_incoming_relations(
        self,
        scope: MemoryScope,
        memory_id: str,
        relation_types: list[RelationType] | None = None,
    ) -> list[MemoryRelation]:
        return await self._relations("to_memory_id", scope, memory_id, relation_types)

    async def _relations(
        self,
        column: str,
        scope: MemoryScope,
        memory_id: str,
        relation_types: list[RelationType] | None,
    ) -> list[MemoryRelation]:
        where = Filter().add("scope_key = ?", scope.scope_key).add(f"{column} = ?", memory_id)
        if relation_types:
            where.add("relation_type = ANY(?)", [r.value for r in relation_types])
        records = await self._pool.fetch(
            f"SELECT * FROM {RELATIONS} WHERE {where.sql}", *where.params
        )
        return [from_row(record, MemoryRelation) for record in records]

    # --- destructive ------------------------------------------------------------- #
    #
    # Nothing in the Dream layer reaches these. They exist for deliberate operator
    # action — a wrong extraction, a "forget that" request, an erasure request —
    # and for tests. Everything here is irreversible.

    async def delete_memories(self, scope: MemoryScope, memory_ids: list[str]) -> int:
        if not memory_ids:
            return 0

        # One transaction: a purge that half-succeeded would leave edges pointing
        # at rows that no longer exist, which is worse than not starting.
        async with self._pool.acquire() as connection, connection.transaction():
            deleted = await connection.fetchval(
                f"WITH removed AS ("
                f"  DELETE FROM {MEMORIES} WHERE memory_id = ANY($1) AND scope_key = $2"
                f"  RETURNING 1"
                f") SELECT count(*) FROM removed",
                memory_ids,
                scope.scope_key,
            )
            await connection.execute(
                f"DELETE FROM {RELATIONS} WHERE scope_key = $1 "
                "AND (from_memory_id = ANY($2) OR to_memory_id = ANY($2))",
                scope.scope_key,
                memory_ids,
            )
            # A memory pointing at something that no longer exists is worse than
            # one pointing at nothing, so clear the inbound pointers too.
            await connection.execute(
                f"UPDATE {MEMORIES} SET superseded_by = NULL "
                "WHERE scope_key = $1 AND superseded_by = ANY($2)",
                scope.scope_key,
                memory_ids,
            )
            await connection.execute(
                f"UPDATE {MEMORIES} SET merged_into = NULL "
                "WHERE scope_key = $1 AND merged_into = ANY($2)",
                scope.scope_key,
                memory_ids,
            )
            await connection.execute(
                f"UPDATE {MEMORIES} SET source_memory_ids = ("
                "  SELECT COALESCE(array_agg(element), '{}') FROM unnest(source_memory_ids)"
                "  AS element WHERE NOT (element = ANY($2))"
                ") WHERE scope_key = $1 AND source_memory_ids && $2",
                scope.scope_key,
                memory_ids,
            )

        logger.warning("memories_hard_deleted", count=deleted, memory_ids=memory_ids[:10])
        return int(deleted)

    async def delete_scope(self, scope: MemoryScope) -> dict[str, int]:
        from app.repositories.postgres.database import JOBS, OPERATIONS

        counts: dict[str, int] = {}
        async with self._pool.acquire() as connection, connection.transaction():
            for label, table in (
                ("memories", MEMORIES),
                ("relations", RELATIONS),
                ("operations", OPERATIONS),
                ("jobs", JOBS),
            ):
                counts[label] = int(
                    await connection.fetchval(
                        f"WITH removed AS ("
                        f"  DELETE FROM {table} WHERE scope_key = $1 RETURNING 1"
                        f") SELECT count(*) FROM removed",
                        scope.scope_key,
                    )
                )

        logger.warning("scope_purged", **counts)
        return counts
