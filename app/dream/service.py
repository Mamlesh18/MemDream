"""Dream job orchestration: create, queue, run, report.

The job row in Mongo is authoritative. The queue only ever carries a job id, so
status is answerable whether the broker is ARQ, Celery or nothing at all, and a
flushed queue loses scheduling but never the record that work was requested.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.exceptions import JobNotFoundError, TenantIsolationError
from app.core.logging import get_logger
from app.dream.synthesis import SynthesisEngine
from app.models.dream import DreamJob
from app.models.enums import JobStatus
from app.models.scope import MemoryScope
from app.providers.queue.base import JobQueue
from app.repositories.job_repository import JobRepository

logger = get_logger(__name__)


class DreamService:
    def __init__(
        self,
        *,
        synthesis: SynthesisEngine,
        jobs: JobRepository,
        queue: JobQueue,
        settings: Settings | None = None,
    ) -> None:
        self._synthesis = synthesis
        self._jobs = jobs
        self._queue = queue
        self._settings = settings or get_settings()

    # --- request -------------------------------------------------------------- #

    async def request_synthesis(
        self,
        scope: MemoryScope,
        *,
        wait: bool = False,
        force: bool = False,
        requested_by: str | None = None,
    ) -> DreamJob:
        """Queue (or run) a synthesis job for one user.

        If a job for this user is already queued or running, that job is returned
        rather than a second one created: re-running synthesis over unchanged
        evidence spends tokens to reach the same NOOPs.
        """
        if not force:
            in_flight = await self._jobs.find_active_for_scope(scope)
            if in_flight is not None:
                logger.info(
                    "synthesis_job_already_active",
                    job_id=in_flight.job_id,
                    status=in_flight.status.value,
                )
                return in_flight

        job = DreamJob(
            client_id=scope.client_id,
            bot_id=scope.bot_id,
            user_id=scope.user_id,
            requested_by=requested_by,
            metadata={"force": force},
        )
        await self._jobs.create(job)

        if wait:
            # Synchronous path for tests, demos and CLI runs. Never used by a
            # user-facing request: synthesis is measured in seconds.
            return await self.run_job(job.job_id, scope, force=force)

        await self._queue.enqueue_synthesis(job)
        return job

    # --- execute ---------------------------------------------------------------- #

    async def run_job(
        self, job_id: str, scope: MemoryScope | None = None, *, force: bool = False
    ) -> DreamJob:
        """Run a queued job to completion and persist its terminal state."""
        job = await self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(f"Dream job {job_id} not found")

        if scope is not None and not (
            job.client_id == scope.client_id
            and job.bot_id == scope.bot_id
            and job.user_id == scope.user_id
        ):
            raise TenantIsolationError("Dream job does not belong to the requesting scope")

        # Rebuild the scope from the job row rather than trusting the caller's:
        # a worker picking this up has only a job id.
        job_scope = MemoryScope(
            client_id=job.client_id, bot_id=job.bot_id, user_id=job.user_id
        )

        await self._jobs.mark_running(job.job_id)
        job.status = JobStatus.RUNNING

        job = await self._synthesis.run(job_scope, job, force=force or job.metadata.get("force", False))
        await self._jobs.complete(job)

        logger.info(
            "synthesis_job_finished",
            job_id=job.job_id,
            status=job.status.value,
            patterns_created=job.patterns_created,
            patterns_updated=job.patterns_updated,
            memories_analyzed=job.memories_analyzed,
            duration_ms=round(job.duration_ms or 0.0, 1),
        )
        return job

    # --- report ------------------------------------------------------------------ #

    async def get_job(self, job_id: str, scope: MemoryScope) -> DreamJob:
        """Fetch a job, enforcing that it belongs to the requesting scope.

        The spec sketch for this endpoint takes only a job id. Requiring the
        scope as well is a deliberate deviation: a bare random id is not an
        authorisation check, and job records disclose how much a user is
        remembered.
        """
        job = await self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(f"Dream job {job_id} not found")

        if not (
            job.client_id == scope.client_id
            and job.bot_id == scope.bot_id
            and job.user_id == scope.user_id
        ):
            # Deliberately indistinguishable from "does not exist".
            raise JobNotFoundError(f"Dream job {job_id} not found")

        return job

    async def list_jobs(
        self, scope: MemoryScope, *, limit: int = 20, offset: int = 0
    ) -> list[DreamJob]:
        return await self._jobs.list_for_scope(scope, limit=limit, offset=offset)
