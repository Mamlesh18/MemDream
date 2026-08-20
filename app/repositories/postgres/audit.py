"""Postgres persistence for the Dream audit log.

The one place the relational store is plainly better than the document store:
`token_usage` and `action_counts` are ordinary `GROUP BY` queries rather than
aggregation pipelines, and the JSONB extraction of per-provider token counts is
a single expression.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.logging import get_logger
from app.models.dream import DreamOperation
from app.models.scope import MemoryScope
from app.repositories.base import AuditRepository
from app.repositories.postgres._sql import Filter, from_row, insert_sql, to_row
from app.repositories.postgres.database import OPERATIONS, PostgresDatabase

logger = get_logger(__name__)

OPERATION_COLUMNS: tuple[str, ...] = (
    "operation_id",
    "scope_key",
    "user_id",
    "agent_id",
    "client_id",
    "namespace",
    "action",
    "memory_id",
    "target_memory_ids",
    "reason",
    "input_content",
    "candidate_memory_ids",
    "model",
    "provider",
    "confidence",
    "request_id",
    "job_id",
    "latency_ms",
    "token_usage",
    "validation_notes",
    "timestamp",
)


class PostgresAuditRepository(AuditRepository):
    def __init__(self, database: PostgresDatabase) -> None:
        self._db = database

    @property
    def _pool(self) -> Any:
        return self._db.pool

    async def record(self, operation: DreamOperation) -> DreamOperation:
        try:
            await self._pool.execute(
                insert_sql(OPERATIONS, OPERATION_COLUMNS),
                *to_row(operation, OPERATION_COLUMNS),
            )
        except Exception as exc:  # noqa: BLE001
            # The memory is already committed. Failing here would report a false
            # error for an operation that actually succeeded.
            logger.error(
                "audit_write_failed",
                operation_id=operation.operation_id,
                action=operation.action,
                error=str(exc)[:300],
            )
        return operation

    async def get(self, operation_id: str) -> DreamOperation | None:
        record = await self._pool.fetchrow(
            f"SELECT * FROM {OPERATIONS} WHERE operation_id = $1", operation_id
        )
        return from_row(record, DreamOperation) if record else None

    async def list_for_scope(
        self,
        scope: MemoryScope,
        *,
        limit: int = 50,
        offset: int = 0,
        actions: list[str] | None = None,
        since: datetime | None = None,
    ) -> list[DreamOperation]:
        where = Filter().add("scope_key = ?", scope.scope_key)
        if actions:
            where.add("action = ANY(?)", actions)
        if since:
            where.add("timestamp >= ?", since)

        records = await self._pool.fetch(
            f"SELECT * FROM {OPERATIONS} WHERE {where.sql} ORDER BY timestamp DESC "
            f"LIMIT {where.bind(max(1, limit))} OFFSET {where.bind(max(0, offset))}",
            *where.params,
        )
        return [from_row(record, DreamOperation) for record in records]

    async def list_for_memory(self, memory_id: str, limit: int = 50) -> list[DreamOperation]:
        records = await self._pool.fetch(
            f"SELECT * FROM {OPERATIONS} "
            "WHERE memory_id = $1 OR $1 = ANY(target_memory_ids) "
            "ORDER BY timestamp DESC LIMIT $2",
            memory_id,
            limit,
        )
        return [from_row(record, DreamOperation) for record in records]

    async def list_for_job(self, job_id: str, limit: int = 500) -> list[DreamOperation]:
        records = await self._pool.fetch(
            f"SELECT * FROM {OPERATIONS} WHERE job_id = $1 ORDER BY timestamp DESC LIMIT $2",
            job_id,
            limit,
        )
        return [from_row(record, DreamOperation) for record in records]

    async def token_usage(self, scope: MemoryScope) -> dict[str, Any]:
        records = await self._pool.fetch(
            "SELECT action, count(*) AS operations, "
            "  COALESCE(sum((token_usage->>'prompt_tokens')::bigint), 0) AS input_tokens, "
            "  COALESCE(sum((token_usage->>'completion_tokens')::bigint), 0) AS output_tokens "
            f"FROM {OPERATIONS} WHERE scope_key = $1 GROUP BY action",
            scope.scope_key,
        )

        by_action: dict[str, dict[str, int]] = {}
        totals = {"operations": 0, "inputTokens": 0, "outputTokens": 0}
        for record in records:
            entry = {
                "operations": int(record["operations"]),
                "inputTokens": int(record["input_tokens"]),
                "outputTokens": int(record["output_tokens"]),
            }
            by_action[record["action"]] = entry
            for key in totals:
                totals[key] += entry[key]

        totals["totalTokens"] = totals["inputTokens"] + totals["outputTokens"]
        return {"byAction": by_action, "totals": totals}

    async def action_counts(self, scope: MemoryScope) -> dict[str, int]:
        records = await self._pool.fetch(
            f"SELECT action, count(*) AS count FROM {OPERATIONS} "
            "WHERE scope_key = $1 GROUP BY action",
            scope.scope_key,
        )
        return {record["action"]: int(record["count"]) for record in records}
