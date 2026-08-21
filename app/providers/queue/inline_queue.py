"""Synthesis inside the API process. One service, no broker, no worker.

This is the default, and for most deployments it should stay that way.

## What the queue is actually for

Synthesis makes one LLM call per cluster and takes seconds. The write that
triggers it — `POST /v1/memories` — has to return in milliseconds. So there is
exactly one hard requirement: **the work must be detached from the request that
caused it.** Nothing else about a queue is fundamental here.

"Detached" does not have to mean "another process". This module detaches it into
a bounded pool of asyncio tasks in the same process, which satisfies the
requirement with no second deployment and nothing to install. `database` exists
for deployments that outgrow one process; it is not a starting point.

## Why this is safe for a workload most background jobs are not

Synthesis has two properties that make in-process execution reasonable, and both
have to hold or this would be a bad idea:

**It is idempotent.** Running it twice over the same evidence produces one
pattern, not two — the synthesiser checks source overlap and semantic similarity
before writing anything.

**Its trigger is derived from durable state.** "This user has N new memories
since their last completed run" is a fact in the database, not an event you can
miss. So a job lost to a restart costs nothing: the condition that caused it is
still true, and the next write re-creates it.

Together those mean losing a job is not losing work. That is the opposite of,
say, a payment webhook — which is what delivery guarantees are actually for.

It is also LLM-bound. Almost its entire duration is spent awaiting the network,
which on an event loop costs essentially nothing; the only real CPU is
clustering a few hundred vectors, measured in milliseconds. It does not starve
request handling.

## The bounded pool

Fire-and-forget `create_task` per job would be simpler and wrong: fifty calls to
`POST /v1/dream/synthesize` would open fifty concurrent LLM streams and turn a
traffic spike into a rate-limit incident. So work goes through a fixed number of
workers reading a bounded queue.

When the queue is full the job is **failed immediately** rather than left
waiting. That looks harsh but is the kinder option: a row sitting at `queued`
counts as in-flight, which would block that user's synthesis for the whole stale
timeout. Failing it frees the user, and because the trigger is derived from
durable state, the next write queues it again.

## What you give up

A job interrupted by a restart is not resumed — it re-fires on the next write
instead. If that is not acceptable, or you are running several API replicas and
want one shared queue, move to `DREAM_QUEUE_PROVIDER=database` — the same idea
with the jobs table as the queue and a worker process claiming rows. Still no
broker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.logging import get_logger
from app.models.dream import DreamJob
from app.providers.queue.base import JobQueue

logger = get_logger(__name__)

#: Takes a job id and nothing else. The job row is the authority on its own
#: scope, so a runner has no use for the tenancy fields it used to be passed.
SynthesisRunner = Callable[[str], Awaitable[None]]

#: How long shutdown waits for in-flight synthesis before giving up on it.
#: Long enough for a job that is nearly done, short enough not to hold a deploy.
DEFAULT_DRAIN_SECONDS = 30.0


class EmbeddedJobQueue(JobQueue):
    """A bounded pool of synthesis workers running in the API process."""

    def __init__(
        self,
        runner: SynthesisRunner | None = None,
        *,
        max_concurrent: int = 2,
        max_queued: int = 100,
        drain_seconds: float = DEFAULT_DRAIN_SECONDS,
    ) -> None:
        self._runner = runner
        self._jobs: Any = None
        self._max_concurrent = max(1, max_concurrent)
        self._max_queued = max(1, max_queued)
        self._drain_seconds = drain_seconds

        # Created on first use: constructing an asyncio.Queue binds it to the
        # running loop, and the container is built before there reliably is one.
        self._queue: asyncio.Queue | None = None
        self._workers: list[asyncio.Task] = []
        self._in_flight = 0
        self._closing = False

    @property
    def name(self) -> str:
        return "inline"

    # --- wiring ---------------------------------------------------------------- #

    def set_runner(self, runner: SynthesisRunner) -> None:
        """Wired after construction: the runner needs the container that owns
        this queue, so the dependency is necessarily circular at build time."""
        self._runner = runner

    def bind(self, runner: SynthesisRunner, jobs: Any = None) -> None:
        """Attach the runner and, optionally, the job repository.

        The repository is what lets a rejected or abandoned job be marked
        `failed` with a reason. Without it those rows stay `queued` or `running`
        until the stale reaper gets to them, which blocks that user's next
        synthesis in the meantime.
        """
        self._runner = runner
        self._jobs = jobs

    # --- pool ------------------------------------------------------------------ #

    def _ensure_started(self) -> asyncio.Queue:
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self._max_queued)
            self._workers = [
                asyncio.create_task(self._worker(index), name=f"synthesis-worker-{index}")
                for index in range(self._max_concurrent)
            ]
            logger.info(
                "synthesis_pool_started",
                workers=self._max_concurrent,
                max_queued=self._max_queued,
            )
        return self._queue

    async def _worker(self, index: int) -> None:
        assert self._queue is not None
        while True:
            job_id = await self._queue.get()
            self._in_flight += 1
            try:
                await self._runner(job_id)  # type: ignore[misc]
            except asyncio.CancelledError:
                # Shutdown gave up waiting. Record it so the row does not sit at
                # `running` until the stale timeout.
                await self._abandon(job_id, "worker cancelled during shutdown")
                raise
            except Exception as exc:  # noqa: BLE001
                # `run_job` already records the failure on the row; this is so an
                # error in a detached task is not silent in the logs.
                logger.exception(
                    "synthesis_job_failed", job_id=job_id, worker=index, error=str(exc)[:300]
                )
            finally:
                self._in_flight -= 1
                self._queue.task_done()

    async def _abandon(self, job_id: str, reason: str) -> None:
        if self._jobs is None:
            logger.warning("synthesis_job_abandoned", job_id=job_id, reason=reason)
            return
        try:
            await self._jobs.fail(job_id, reason)
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning("synthesis_job_abandon_failed", job_id=job_id, error=str(exc)[:200])

    # --- API -------------------------------------------------------------------- #

    async def enqueue_synthesis(self, job: DreamJob) -> str:
        if self._runner is None:
            raise RuntimeError("EmbeddedJobQueue has no runner configured")
        if self._closing:
            await self._abandon(job.job_id, "service is shutting down")
            return job.job_id

        queue = self._ensure_started()
        try:
            queue.put_nowait(job.job_id)
        except asyncio.QueueFull:
            # Backpressure. Failing the row rather than leaving it queued is
            # what keeps the user unblocked: a queued row reads as in-flight,
            # and would stop their next write re-triggering for the whole stale
            # timeout.
            logger.warning(
                "synthesis_queue_saturated",
                job_id=job.job_id,
                queued=queue.qsize(),
                max_queued=self._max_queued,
            )
            await self._abandon(
                job.job_id,
                "synthesis queue is saturated; it will be retried on the next write",
            )
            return job.job_id

        logger.info("synthesis_job_queued", job_id=job.job_id, depth=queue.qsize())
        return job.job_id

    async def wait_for_all(self, timeout: float = 120.0) -> None:
        """Block until queued work finishes. Test and demo helper."""
        if self._queue is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("synthesis_wait_timed_out", timeout=timeout)

    @property
    def depth(self) -> int:
        """Jobs waiting plus jobs running. Reported in health output."""
        waiting = self._queue.qsize() if self._queue is not None else 0
        return waiting + self._in_flight

    async def health_check(self) -> bool:
        # Nothing external to reach. Saturation is visible through `depth`
        # rather than by reporting the queue unhealthy — it is working hard,
        # not broken.
        return True

    async def close(self) -> None:
        """Drain in-flight synthesis, then stop.

        Cancelling immediately would leave rows at `running` for the stale
        timeout, so a deploy would block those users' synthesis for fifteen
        minutes. Waiting a bounded time and failing whatever is left with a
        reason is both faster to recover from and easier to read.
        """
        self._closing = True
        if self._queue is None:
            return

        if self.depth:
            logger.info("synthesis_pool_draining", depth=self.depth, timeout=self._drain_seconds)
            try:
                await asyncio.wait_for(self._queue.join(), timeout=self._drain_seconds)
            except asyncio.TimeoutError:
                logger.warning("synthesis_drain_timed_out", remaining=self.depth)

        # Anything still queued never started, so it can be failed directly.
        while not self._queue.empty():
            try:
                await self._abandon(self._queue.get_nowait(), "service stopped before it started")
                self._queue.task_done()
            except asyncio.QueueEmpty:  # pragma: no cover - race with a worker
                break

        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)

        self._workers.clear()
        self._queue = None
        logger.info("synthesis_pool_stopped")


#: The name this queue had when it ran one unbounded task per job.
InlineJobQueue = EmbeddedJobQueue
