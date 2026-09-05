"""Dream job orchestration: create, queue, run, report.

The job row in Mongo is authoritative. The queue only ever carries a job id, so
status is answerable whether synthesis runs in this process or another, and a
flushed queue loses scheduling but never the record that work was requested.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.exceptions import JobNotFoundError, TenantIsolationError
from app.core.logging import get_logger
from app.dream.consolidation import ConsolidationEngine
from app.dream.synthesis import SynthesisEngine
from app.models.dream import DreamJob
from app.models.enums import JobStatus
from app.models.memory import utcnow
from app.models.scope import MemoryScope
from app.providers.queue.base import JobQueue
from app.repositories.base import JobRepository, MemoryRepository

logger = get_logger(__name__)


class DreamService:
    def __init__(
        self,
        *,
        synthesis: SynthesisEngine,
        consolidation: ConsolidationEngine | None = None,
        jobs: JobRepository,
        memories: MemoryRepository,
        queue: JobQueue,
        settings: Settings | None = None,
    ) -> None:
        self._synthesis = synthesis
        self._consolidation = consolidation
        self._jobs = jobs
        self._memories = memories
        self._queue = queue
        self._settings = settings or get_settings()

    # --- request -------------------------------------------------------------- #

    async def request_synthesis(
        self,
        scope: MemoryScope,
        *,
        wait: bool = False,
        force: bool = False,
        min_memories: int | None = None,
        requested_by: str | None = None,
    ) -> DreamJob:
        """Queue (or run) a synthesis job for one user.

        If a job for this user is already queued or running, that job is returned
        rather than a second one created: re-running synthesis over unchanged
        evidence spends tokens to reach the same NOOPs.
        """
        if not force:
            in_flight = await self._jobs.find_active_for_scope(
                scope, stale_after_seconds=self._settings.dream.synthesis.job_stale_after_seconds
            )
            if in_flight is not None:
                logger.info(
                    "synthesis_job_already_active",
                    job_id=in_flight.job_id,
                    status=in_flight.status.value,
                )
                return in_flight

        # Options ride on the row, not on the call. With the `database` queue the
        # process that runs this job is not the one that created it, so an
        # argument would not survive the hop.
        job = DreamJob(
            **scope.as_model_fields(),
            requested_by=requested_by,
            metadata={"force": force, "minMemories": min_memories},
        )
        await self._jobs.create(job)

        if wait:
            # Synchronous path for tests, demos and CLI runs. Never used by a
            # user-facing request: synthesis is measured in seconds.
            return await self.run_job(job.job_id, scope, force=force)

        await self._queue.enqueue_synthesis(job)
        return job

    # --- automatic triggering ------------------------------------------------------ #

    async def maybe_trigger(self, scope: MemoryScope) -> DreamJob | None:
        """Queue synthesis if this user has accumulated enough new material.

        Called after a successful write. Returns the job if one was queued.

        Every gate below reads durable state rather than a counter, so nothing
        can drift out of sync, and a lost job is self-healing: the condition that
        triggered it is still true, so the next write re-evaluates and re-queues.
        That is what lets the whole thing run without a message broker.

        Deliberately never raises. Synthesis is a background nicety; a failure
        here must not fail the user's write.
        """
        config = self._settings.dream.synthesis
        if not (config.enabled and config.auto_trigger):
            return None

        try:
            # 1. Global concurrency cap — a traffic spike must not become a
            #    rate-limit incident on the LLM provider.
            if await self._jobs.running_count() >= config.auto_trigger_max_concurrent:
                return None

            # 2. Already working on this user.
            if await self._jobs.find_active_for_scope(
                scope, stale_after_seconds=config.job_stale_after_seconds
            ):
                return None

            # 3. Cooldown, so a chatty user does not re-derive the same patterns
            #    every few minutes.
            last_run = await self._jobs.last_completed_at(scope)
            if last_run is not None:
                elapsed = (utcnow() - last_run).total_seconds()
                if elapsed < config.auto_trigger_cooldown_seconds:
                    return None

            # 4. Enough NEW material since the last run — not total count, or it
            #    would fire forever once the user crossed the threshold once.
            new_memories = await self._memories.count_since(scope, last_run)
            if new_memories < config.auto_trigger_new_memories:
                return None

            logger.info(
                "synthesis_auto_triggered",
                new_memories=new_memories,
                threshold=config.auto_trigger_new_memories,
            )
            return await self.request_synthesis(scope, requested_by="auto-trigger")

        except Exception as exc:  # noqa: BLE001
            logger.warning("auto_trigger_failed", error=str(exc)[:200])
            return None

    # --- execute ---------------------------------------------------------------- #

    async def run_job(
        self, job_id: str, scope: MemoryScope | None = None, *, force: bool = False
    ) -> DreamJob:
        """Run a queued job to completion and persist its terminal state."""
        job = await self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(f"Dream job {job_id} not found")

        # Rebuild the scope from the job row rather than trusting the caller's:
        # a worker picking this up has only a job id.
        #
        # **Every** dimension, `namespace` included. Rebuilding from the triple
        # alone would compute a scopeKey matching none of this user's memories,
        # and synthesis would quietly analyse nothing for any deployment that
        # partitions by namespace.
        job_scope = MemoryScope(
            user_id=job.user_id,
            agent_id=job.agent_id,
            client_id=job.client_id,
            namespace=job.namespace,
        )

        # Compared as partition identity, for the same reason: two scopes are
        # the same partition when every dimension matches, and `scope_key` is
        # the one value that says so.
        if scope is not None and job_scope.scope_key != scope.scope_key:
            raise TenantIsolationError("Dream job does not belong to the requesting scope")

        await self._jobs.mark_running(job.job_id)
        job.status = JobStatus.RUNNING

        # Consolidate before synthesising. Retiring facts whose validity window
        # has closed means the clusterer works over what is still true, rather
        # than finding a pattern in something that stopped being the case in
        # March. Deterministic and cheap — no model, no embedding.
        if self._consolidation is not None and self._settings.dream.consolidation.enabled:
            try:
                outcome = await self._consolidation.run(job_scope)
                if outcome.expired_count:
                    job.metadata["consolidation"] = outcome.as_dict()
            except Exception as exc:  # noqa: BLE001 - housekeeping must not fail the job
                logger.warning("consolidation_failed", error=str(exc)[:200])

        job = await self._synthesis.run(
            job_scope,
            job,
            force=force or job.metadata.get("force", False),
            min_memories=job.metadata.get("minMemories"),
        )
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

        # Partition identity, not a field-by-field comparison: the triple omits
        # `namespace`, so two different partitions could compare equal and one
        # tenant would read the other's job.
        if job.scope_key != scope.scope_key:
            # Deliberately indistinguishable from "does not exist".
            raise JobNotFoundError(f"Dream job {job_id} not found")

        return job

    async def list_jobs(
        self, scope: MemoryScope, *, limit: int = 20, offset: int = 0
    ) -> list[DreamJob]:
        return await self._jobs.list_for_scope(scope, limit=limit, offset=offset)
