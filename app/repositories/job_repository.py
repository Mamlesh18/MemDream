"""Persistence for Dream jobs.

The job row is the single source of truth for a synthesis run's status. The
queue only carries a job id; everything a caller polls for lives here, so the
API can answer ``GET /v1/dream/jobs/{id}`` without talking to Redis.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pymongo import ASCENDING, DESCENDING, ReturnDocument

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

    async def find_active_for_scope(
        self, scope: MemoryScope, *, stale_after_seconds: int = 900
    ) -> DreamJob | None:
        """A *genuinely* in-flight job for this user, if any.

        Queueing a second run for a user whose first has not finished wastes
        tokens on the same evidence, so callers return the in-flight job instead
        of creating another.

        The staleness cut-off is what makes that safe. A worker that dies
        mid-synthesis leaves its row at `running` forever, and without an age
        check that row would block synthesis for this user permanently — the
        deduplication guard silently becoming a denial of service. Anything
        older than the job timeout is treated as abandoned and ignored, so the
        next request supersedes it.
        """
        cutoff = utcnow() - timedelta(seconds=stale_after_seconds)
        document = await self._db.jobs.find_one(
            {
                **scope.as_filter(),
                "status": {"$in": [JobStatus.QUEUED.value, JobStatus.RUNNING.value]},
                # Compare against whichever timestamp exists: a job that never
                # started is judged on when it was queued.
                "$or": [
                    {"startedAt": {"$gt": cutoff}},
                    {"startedAt": None, "createdAt": {"$gt": cutoff}},
                ],
            },
            sort=[("createdAt", DESCENDING)],
        )
        return DreamJob.from_document(document) if document else None

    async def reap_stale(self, *, stale_after_seconds: int = 900) -> int:
        """Mark abandoned jobs as failed so they stop looking in-flight.

        Housekeeping, not correctness: `find_active_for_scope` already ignores
        them. This exists so operators see `failed` with a reason rather than
        rows stuck at `running` forever.
        """
        cutoff = utcnow() - timedelta(seconds=stale_after_seconds)
        result = await self._db.jobs.update_many(
            {
                "status": {"$in": [JobStatus.QUEUED.value, JobStatus.RUNNING.value]},
                "$or": [
                    {"startedAt": {"$lte": cutoff}},
                    {"startedAt": None, "createdAt": {"$lte": cutoff}},
                ],
            },
            {
                "$set": {
                    "status": JobStatus.FAILED.value,
                    "error": (
                        f"abandoned: no progress for over {stale_after_seconds}s, "
                        "presumed lost to a worker restart"
                    ),
                    "completedAt": utcnow(),
                }
            },
        )
        return result.modified_count

    async def claim_next(self, worker_id: str) -> DreamJob | None:
        """Atomically claim one queued job. Returns None if there are none.

        `find_one_and_update` is a single atomic compare-and-swap, so two
        workers polling simultaneously cannot claim the same job — the second
        one's filter no longer matches. This is what lets MongoDB act as the
        queue without a broker.
        """
        document = await self._db.jobs.find_one_and_update(
            {"status": JobStatus.QUEUED.value},
            {
                "$set": {
                    "status": JobStatus.RUNNING.value,
                    "startedAt": utcnow(),
                    "metadata.claimedBy": worker_id,
                }
            },
            sort=[("createdAt", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        return DreamJob.from_document(document) if document else None

    async def last_completed_at(self, scope: MemoryScope):
        """When this user's most recent synthesis finished, or None."""
        document = await self._db.jobs.find_one(
            {**scope.as_filter(), "status": JobStatus.COMPLETED.value},
            sort=[("completedAt", DESCENDING)],
            projection={"completedAt": 1},
        )
        return document.get("completedAt") if document else None

    async def running_count(self) -> int:
        """Synthesis jobs in flight across all users, for the concurrency cap."""
        return await self._db.jobs.count_documents(
            {"status": {"$in": [JobStatus.QUEUED.value, JobStatus.RUNNING.value]}}
        )

    async def stats(self) -> dict[str, Any]:
        pipeline = [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
        cursor = self._db.jobs.aggregate(pipeline)
        return {doc["_id"]: doc["count"] async for doc in cursor}
