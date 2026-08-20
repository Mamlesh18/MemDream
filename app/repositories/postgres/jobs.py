"""Postgres persistence for Dream jobs.

`claim_next` is the interesting one. MongoDB claims a job with an atomic
`find_one_and_update` compare-and-swap; Postgres uses `FOR UPDATE SKIP LOCKED`,
which is strictly better for the purpose. Two workers polling simultaneously do
not contend at all — the second skips the row the first has locked rather than
retrying against a filter that no longer matches. It is the canonical Postgres
job-queue pattern, and it is why the `mongo` queue provider has a `postgres`
equivalent for free: the queue is the jobs table either way.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.models.dream import DreamJob
from app.models.enums import JobStatus
from app.models.memory import utcnow
from app.models.scope import MemoryScope
from app.repositories.base import JobRepository
from app.repositories.postgres._sql import Filter, from_row, insert_sql, to_row, upsert_sql
from app.repositories.postgres.database import JOBS, PostgresDatabase

JOB_COLUMNS: tuple[str, ...] = (
    "job_id",
    "scope_key",
    "user_id",
    "agent_id",
    "client_id",
    "namespace",
    "status",
    "job_type",
    "memories_analyzed",
    "clusters_evaluated",
    "patterns_created",
    "patterns_updated",
    "patterns_skipped",
    "noop_decisions",
    "error",
    "created_pattern_ids",
    "created_at",
    "started_at",
    "completed_at",
    "duration_ms",
    "requested_by",
    "metadata",
)

_ACTIVE_STATUSES = [JobStatus.QUEUED.value, JobStatus.RUNNING.value]


class PostgresJobRepository(JobRepository):
    def __init__(self, database: PostgresDatabase) -> None:
        self._db = database

    @property
    def _pool(self) -> Any:
        return self._db.pool

    async def create(self, job: DreamJob) -> DreamJob:
        await self._pool.execute(insert_sql(JOBS, JOB_COLUMNS), *to_row(job, JOB_COLUMNS))
        return job

    async def get(self, job_id: str) -> DreamJob | None:
        record = await self._pool.fetchrow(f"SELECT * FROM {JOBS} WHERE job_id = $1", job_id)
        return from_row(record, DreamJob) if record else None

    async def mark_running(self, job_id: str) -> None:
        await self._pool.execute(
            f"UPDATE {JOBS} SET status = $1, started_at = $2 WHERE job_id = $3",
            JobStatus.RUNNING.value,
            utcnow(),
            job_id,
        )

    async def complete(self, job: DreamJob) -> DreamJob:
        job.completed_at = utcnow()
        started = job.started_at or job.created_at
        # Computed from the row's own timestamps, so a worker restart cannot
        # produce a negative elapsed time.
        job.duration_ms = (job.completed_at - started).total_seconds() * 1000.0
        await self._pool.execute(
            upsert_sql(JOBS, JOB_COLUMNS, "job_id"), *to_row(job, JOB_COLUMNS)
        )
        return job

    async def fail(self, job_id: str, error: str) -> None:
        await self._pool.execute(
            f"UPDATE {JOBS} SET status = $1, error = $2, completed_at = $3 WHERE job_id = $4",
            JobStatus.FAILED.value,
            # Truncated: a stack trace belongs in the logs, not in a field that
            # gets returned to API callers.
            error[:2000],
            utcnow(),
            job_id,
        )

    async def list_for_scope(
        self, scope: MemoryScope, *, limit: int = 20, offset: int = 0
    ) -> list[DreamJob]:
        where = Filter().add("scope_key = ?", scope.scope_key)
        records = await self._pool.fetch(
            f"SELECT * FROM {JOBS} WHERE {where.sql} ORDER BY created_at DESC "
            f"LIMIT {where.bind(max(1, limit))} OFFSET {where.bind(max(0, offset))}",
            *where.params,
        )
        return [from_row(record, DreamJob) for record in records]

    async def find_active_for_scope(
        self, scope: MemoryScope, *, stale_after_seconds: int = 900
    ) -> DreamJob | None:
        cutoff = utcnow() - timedelta(seconds=stale_after_seconds)
        record = await self._pool.fetchrow(
            f"SELECT * FROM {JOBS} WHERE scope_key = $1 AND status = ANY($2) "
            # Judge a job that never started on when it was queued.
            "  AND COALESCE(started_at, created_at) > $3 "
            "ORDER BY created_at DESC LIMIT 1",
            scope.scope_key,
            _ACTIVE_STATUSES,
            cutoff,
        )
        return from_row(record, DreamJob) if record else None

    async def reap_stale(self, *, stale_after_seconds: int = 900) -> int:
        cutoff = utcnow() - timedelta(seconds=stale_after_seconds)
        status = await self._pool.execute(
            f"UPDATE {JOBS} SET status = $1, error = $2, completed_at = $3 "
            "WHERE status = ANY($4) AND COALESCE(started_at, created_at) <= $5",
            JobStatus.FAILED.value,
            (
                f"abandoned: no progress for over {stale_after_seconds}s, "
                "presumed lost to a worker restart"
            ),
            utcnow(),
            _ACTIVE_STATUSES,
            cutoff,
        )
        return int(status.rsplit(" ", 1)[-1]) if status else 0

    async def claim_next(self, worker_id: str) -> DreamJob | None:
        """Atomically claim one queued job. Returns None if there are none.

        `SKIP LOCKED` is what makes this contention-free: a second worker steps
        over the row the first has locked instead of blocking on it or retrying.
        """
        record = await self._pool.fetchrow(
            f"UPDATE {JOBS} SET status = $1, started_at = $2, "
            "    metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), "
            "                         '{claimedBy}', to_jsonb($3::text), true) "
            f"WHERE job_id = ("
            f"    SELECT job_id FROM {JOBS} WHERE status = $4 "
            "     ORDER BY created_at ASC FOR UPDATE SKIP LOCKED LIMIT 1"
            ") RETURNING *",
            JobStatus.RUNNING.value,
            utcnow(),
            worker_id,
            JobStatus.QUEUED.value,
        )
        return from_row(record, DreamJob) if record else None

    async def last_completed_at(self, scope: MemoryScope) -> datetime | None:
        return await self._pool.fetchval(
            f"SELECT completed_at FROM {JOBS} WHERE scope_key = $1 AND status = $2 "
            "ORDER BY completed_at DESC LIMIT 1",
            scope.scope_key,
            JobStatus.COMPLETED.value,
        )

    async def running_count(self) -> int:
        return await self._pool.fetchval(
            f"SELECT count(*) FROM {JOBS} WHERE status = ANY($1)", _ACTIVE_STATUSES
        )

    async def stats(self) -> dict[str, Any]:
        records = await self._pool.fetch(
            f"SELECT status, count(*) AS count FROM {JOBS} GROUP BY status"
        )
        return {record["status"]: int(record["count"]) for record in records}
