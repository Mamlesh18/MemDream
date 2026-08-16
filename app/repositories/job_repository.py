"""Persistence for Dream jobs.

The job row is the single source of truth for a synthesis run's status. The
queue only carries a job id; everything a caller polls for lives here, so the
API can answer ``GET /v1/dream/jobs/{id}`` without talking to Redis.
"""

from __future__ import annotations

from typing import Any

from pymongo import DESCENDING

from app.models.dream import DreamJob
from app.models.enums import JobStatus
from app.models.memory import utcnow
from app.models.scope import MemoryScope
from app.repositories.mongo import MongoDatabase


class JobRepository:
    def __init__(self, database: MongoDatabase) -> None:
        self._db = database

    async def create(self, job: DreamJob) -> DreamJob:
        await self._db.jobs.insert_one(job.to_document())
        return job

    async def get(self, job_id: str) -> DreamJob | None:
        document = await self._db.jobs.find_one({"_id": job_id})
        return DreamJob.from_document(document) if document else None

    async def mark_running(self, job_id: str) -> None:
        await self._db.jobs.update_one(
            {"_id": job_id},
            {"$set": {"status": JobStatus.RUNNING.value, "startedAt": utcnow()}},
        )

    async def complete(self, job: DreamJob) -> DreamJob:
        """Write the terminal state, computing duration from the row's own
        timestamps so a worker restart cannot produce a negative elapsed time."""
        job.completed_at = utcnow()
        started = job.started_at or job.created_at
        job.duration_ms = (job.completed_at - started).total_seconds() * 1000.0
        await self._db.jobs.replace_one({"_id": job.job_id}, job.to_document())
        return job

    async def fail(self, job_id: str, error: str) -> None:
        await self._db.jobs.update_one(
            {"_id": job_id},
            {
                "$set": {
                    "status": JobStatus.FAILED.value,
                    # Truncated: a stack trace belongs in the logs, not in a
                    # field that gets returned to API callers.
                    "error": error[:2000],
                    "completedAt": utcnow(),
                }
            },
        )

    async def list_for_scope(
        self, scope: MemoryScope, *, limit: int = 20, offset: int = 0
    ) -> list[DreamJob]:
        cursor = (
            self._db.jobs.find(scope.as_filter())
            .sort("createdAt", DESCENDING)
            .skip(max(0, offset))
            .limit(max(1, limit))
        )
        return [DreamJob.from_document(doc) async for doc in cursor]

    async def find_active_for_scope(self, scope: MemoryScope) -> DreamJob | None:
        """An already-queued or running job for this user, if any.

        Queueing a second synthesis run for a user whose first has not finished
        wastes tokens on the same evidence, so the API returns the in-flight job
        instead of creating another.
        """
        document = await self._db.jobs.find_one(
            {
                **scope.as_filter(),
                "status": {"$in": [JobStatus.QUEUED.value, JobStatus.RUNNING.value]},
            },
            sort=[("createdAt", DESCENDING)],
        )
        return DreamJob.from_document(document) if document else None

    async def stats(self) -> dict[str, Any]:
        pipeline = [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
        cursor = self._db.jobs.aggregate(pipeline)
        return {doc["_id"]: doc["count"] async for doc in cursor}
