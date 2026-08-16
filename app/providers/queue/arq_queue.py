"""ARQ (Redis) job queue.

ARQ rather than Celery because this stack is async end to end — Motor, httpx to
Azure OpenAI, the FAISS thread offload. An ARQ worker runs the same coroutines
the API does, with no `asyncio.run()` bridge inside a sync task and no second
code path to keep correct.
"""

from __future__ import annotations

from typing import Any

from app.core.exceptions import ProviderError
from app.core.logging import get_logger
from app.models.dream import DreamJob
from app.providers.queue.base import JobQueue

logger = get_logger(__name__)

#: Must match the coroutine name registered in `app.workers.synthesis_worker`.
SYNTHESIS_TASK = "run_synthesis_task"


class ArqJobQueue(JobQueue):
    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._pool: Any = None

    @property
    def name(self) -> str:
        return "arq"

    async def _get_pool(self) -> Any:
        """Connect lazily so the API boots even when Redis is briefly down."""
        if self._pool is not None:
            return self._pool
        try:
            from arq import create_pool
            from arq.connections import RedisSettings
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ProviderError("The 'arq' package is required for the ARQ queue") from exc

        try:
            self._pool = await create_pool(RedisSettings.from_dsn(self._redis_url))
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"Cannot connect to Redis at {self._redis_url}: {exc}") from exc
        return self._pool

    async def enqueue_synthesis(self, job: DreamJob) -> str:
        pool = await self._get_pool()
        await pool.enqueue_job(
            SYNTHESIS_TASK,
            job.job_id,
            job.client_id,
            job.bot_id,
            job.user_id,
            # ARQ deduplicates by job id, so a double-submitted job runs once.
            _job_id=f"synthesis:{job.job_id}",
        )
        logger.info("synthesis_job_enqueued", job_id=job.job_id, queue="arq")
        return job.job_id

    async def health_check(self) -> bool:
        try:
            pool = await self._get_pool()
            await pool.ping()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("arq_health_check_failed", error=str(exc)[:200])
            return False

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None
