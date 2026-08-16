"""In-process job queue.

Runs synthesis as a background asyncio task in the API process. No Redis, no
worker container — which makes the whole framework runnable with nothing but a
MongoDB instance, and makes tests deterministic via `wait_for_all()`.

Not for production: work is lost if the process restarts mid-job, and synthesis
competes with request handling for the same event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.core.logging import get_logger
from app.models.dream import DreamJob
from app.providers.queue.base import JobQueue

logger = get_logger(__name__)

SynthesisRunner = Callable[[str, str, str, str], Awaitable[None]]


class InlineJobQueue(JobQueue):
    def __init__(self, runner: SynthesisRunner | None = None) -> None:
        self._runner = runner
        # Strong references: asyncio only weakly references running tasks, so a
        # fire-and-forget task can be garbage collected mid-flight without this.
        self._tasks: set[asyncio.Task] = set()

    @property
    def name(self) -> str:
        return "inline"

    def set_runner(self, runner: SynthesisRunner) -> None:
        """Wired after construction — the runner needs the container that owns
        this queue, so the dependency is necessarily circular at build time."""
        self._runner = runner

    async def enqueue_synthesis(self, job: DreamJob) -> str:
        if self._runner is None:
            raise RuntimeError("InlineJobQueue has no runner configured")

        task = asyncio.create_task(
            self._run(job.job_id, job.client_id, job.bot_id, job.user_id),
            name=f"synthesis:{job.job_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info("synthesis_job_started_inline", job_id=job.job_id)
        return job.job_id

    async def _run(self, job_id: str, client_id: str, bot_id: str, user_id: str) -> None:
        try:
            await self._runner(job_id, client_id, bot_id, user_id)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001
            # The job row already records the failure; this is just so an
            # unhandled error in a detached task is not silent.
            logger.exception("inline_synthesis_failed", job_id=job_id, error=str(exc)[:300])

    async def wait_for_all(self, timeout: float = 120.0) -> None:
        """Block until queued jobs finish. Test and demo helper."""
        if not self._tasks:
            return
        await asyncio.wait(set(self._tasks), timeout=timeout)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
