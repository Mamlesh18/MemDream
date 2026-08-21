"""The canonical store *is* the queue — no broker required.

Synthesis has two properties that make a dedicated message broker unnecessary:

**It is idempotent.** Running it twice over the same evidence produces one
pattern, not two.

**Its trigger is derivable from durable state.** "This user has N new memories
since their last synthesis" is a fact sitting in the database, not an event you
can miss.

Together those mean a lost job costs nothing: the condition that caused it is
still true, so it is re-created on the next write. That is the opposite of, say,
a payment webhook, where losing the message loses money — which is what delivery
guarantees are actually for.

So the queue is a table you already have. Claiming is `JobRepository.claim_next`,
which every backend implements atomically — a compare-and-swap on MongoDB,
`FOR UPDATE SKIP LOCKED` on Postgres. Two workers polling at the same instant
cannot claim the same row either way.

That is also why this queue is not named after a database: it holds a
`JobRepository`, so it works with whichever canonical store is configured.

Trade-off against `inline`: a second process to deploy. What you get for it is a
queue shared across API replicas and a job that survives the process that
queued it restarting. If you do not need either, do not run this.
"""

from __future__ import annotations

import asyncio
import os
import socket

from app.core.logging import get_logger
from app.models.dream import DreamJob
from app.providers.queue.base import JobQueue
from app.repositories.base import JobRepository

logger = get_logger(__name__)


class DatabaseJobQueue(JobQueue):
    """Enqueue by inserting a row; the worker polls and claims."""

    def __init__(self, jobs: JobRepository, *, poll_interval: float = 5.0) -> None:
        self._jobs = jobs
        self._poll_interval = poll_interval
        # Recorded on the claimed job so an operator can tell which process is
        # working it, and which one died if it goes stale.
        self._worker_id = f"{socket.gethostname()}:{os.getpid()}"

    @property
    def name(self) -> str:
        # Not "mongo": this queue holds a JobRepository, so it is whichever
        # canonical store is configured. Reported in /ready and the console.
        return "database"

    async def enqueue_synthesis(self, job: DreamJob) -> str:
        """No-op beyond logging: the job row *is* the queue entry.

        `DreamService` has already written the row with `status=queued` by the
        time this is called, so there is nothing to publish.
        """
        logger.info("synthesis_job_enqueued", job_id=job.job_id, queue="database")
        return job.job_id

    async def claim_next(self) -> DreamJob | None:
        """Atomically take ownership of one queued job."""
        return await self._jobs.claim_next(self._worker_id)

    async def poll_forever(self, handler, *, stop: asyncio.Event | None = None) -> None:
        """Claim and run jobs until stopped. This is the worker loop.

        `handler` takes a `DreamJob`. Failures are logged and the loop continues:
        one bad job must not stop the queue.
        """
        logger.info("queue_worker_started", worker=self._worker_id)
        while stop is None or not stop.is_set():
            try:
                job = await self.claim_next()
            except Exception as exc:  # noqa: BLE001 - a dropped connection must not kill the loop
                logger.error("job_claim_failed", error=str(exc)[:200])
                await asyncio.sleep(self._poll_interval)
                continue

            if job is None:
                # Nothing waiting. Sleep rather than hot-loop the database.
                try:
                    if stop is not None:
                        await asyncio.wait_for(stop.wait(), timeout=self._poll_interval)
                        break
                    await asyncio.sleep(self._poll_interval)
                except asyncio.TimeoutError:
                    pass
                continue

            try:
                await handler(job)
            except Exception as exc:  # noqa: BLE001
                logger.exception("synthesis_job_failed", job_id=job.job_id, error=str(exc)[:300])
                await self._jobs.fail(job.job_id, str(exc))

    async def health_check(self) -> bool:
        try:
            await self._jobs.stats()
            return True
        except Exception:  # noqa: BLE001
            return False


#: Previous name, from when MongoDB was the only canonical store.
MongoJobQueue = DatabaseJobQueue
