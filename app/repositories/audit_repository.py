"""Persistence for the Dream audit log.

Every Dream decision writes a row here — including NOOPs, which are the ones you
want when a threshold is misconfigured and nothing is being remembered.

Audit writes must never fail a memory operation: the memory is already committed
by the time we get here, so a logging failure is logged and swallowed rather
than propagated back to the caller as a false error.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pymongo import DESCENDING

from app.core.logging import get_logger
from app.models.dream import DreamOperation
from app.models.scope import MemoryScope
from app.repositories.mongo import MongoDatabase

logger = get_logger(__name__)


class AuditRepository:
    def __init__(self, database: MongoDatabase) -> None:
        self._db = database

    async def record(self, operation: DreamOperation) -> DreamOperation:
        try:
            await self._db.operations.insert_one(operation.to_document())
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "audit_write_failed",
                operation_id=operation.operation_id,
                action=operation.action,
                error=str(exc)[:300],
            )
        return operation

    async def get(self, operation_id: str) -> DreamOperation | None:
        document = await self._db.operations.find_one({"_id": operation_id})
        return DreamOperation.from_document(document) if document else None

    async def list_for_scope(
        self,
        scope: MemoryScope,
        *,
        limit: int = 50,
        offset: int = 0,
        actions: list[str] | None = None,
        since: datetime | None = None,
    ) -> list[DreamOperation]:
        query: dict[str, Any] = scope.as_filter()
        if actions:
            query["action"] = {"$in": actions}
        if since:
            query["timestamp"] = {"$gte": since}

        cursor = (
            self._db.operations.find(query)
            .sort("timestamp", DESCENDING)
            .skip(max(0, offset))
            .limit(max(1, limit))
        )
        return [DreamOperation.from_document(doc) async for doc in cursor]

    async def list_for_memory(self, memory_id: str, limit: int = 50) -> list[DreamOperation]:
        """The decision history of one memory, newest first."""
        cursor = (
            self._db.operations.find(
                {"$or": [{"memoryId": memory_id}, {"targetMemoryIds": memory_id}]}
            )
            .sort("timestamp", DESCENDING)
            .limit(limit)
        )
        return [DreamOperation.from_document(doc) async for doc in cursor]

    async def list_for_job(self, job_id: str, limit: int = 500) -> list[DreamOperation]:
        cursor = (
            self._db.operations.find({"jobId": job_id}).sort("timestamp", DESCENDING).limit(limit)
        )
        return [DreamOperation.from_document(doc) async for doc in cursor]

    async def token_usage(self, scope: MemoryScope) -> dict[str, Any]:
        """Total tokens spent on one user, summed from their audit rows.

        Durable and per-user, unlike the in-process metrics registry: it survives
        restarts and is correct across replicas, because every Dream decision
        records what it cost on the row it wrote.
        """
        pipeline = [
            {"$match": scope.as_filter()},
            {
                "$group": {
                    "_id": "$action",
                    "operations": {"$sum": 1},
                    "inputTokens": {"$sum": {"$ifNull": ["$tokenUsage.prompt_tokens", 0]}},
                    "outputTokens": {"$sum": {"$ifNull": ["$tokenUsage.completion_tokens", 0]}},
                }
            },
        ]
        cursor = self._db.operations.aggregate(pipeline)

        by_action: dict[str, dict[str, int]] = {}
        totals = {"operations": 0, "inputTokens": 0, "outputTokens": 0}
        async for row in cursor:
            entry = {
                "operations": row.get("operations", 0),
                "inputTokens": row.get("inputTokens", 0),
                "outputTokens": row.get("outputTokens", 0),
            }
            by_action[row["_id"]] = entry
            for key in totals:
                totals[key] += entry[key]

        totals["totalTokens"] = totals["inputTokens"] + totals["outputTokens"]
        return {"byAction": by_action, "totals": totals}

    async def action_counts(self, scope: MemoryScope) -> dict[str, int]:
        """How often each action fired for a user — the first thing to look at
        when tuning merge/supersede thresholds."""
        pipeline = [
            {"$match": scope.as_filter()},
            {"$group": {"_id": "$action", "count": {"$sum": 1}}},
        ]
        cursor = self._db.operations.aggregate(pipeline)
        return {doc["_id"]: doc["count"] async for doc in cursor}
