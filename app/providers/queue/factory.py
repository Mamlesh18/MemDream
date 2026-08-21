from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError
from app.providers.queue.base import JobQueue


def build_job_queue(settings: Settings | None = None, jobs=None) -> JobQueue:
    """Construct the queue named by ``DREAM_QUEUE_PROVIDER``."""
    settings = settings or get_settings()
    name = settings.queue_provider

    # "mongo" is the previous spelling, from when MongoDB was the only store.
    if name in ("database", "mongo"):
        from app.providers.queue.database_queue import DatabaseJobQueue

        if jobs is None:
            raise ConfigurationError(
                "The database queue needs a JobRepository. Build it through "
                "DreamContainer.create, which already has one."
            )
        return DatabaseJobQueue(jobs)

    # "inline" runs synthesis in this process, in a bounded pool. Default,
    # because it needs no second service and no broker.
    if name in ("inline", "embedded"):
        from app.providers.queue.inline_queue import EmbeddedJobQueue

        synthesis = settings.dream.synthesis
        return EmbeddedJobQueue(
            max_concurrent=synthesis.max_concurrent,
            max_queued=synthesis.max_queued,
            drain_seconds=synthesis.drain_seconds,
        )

    raise ConfigurationError(f"Unknown queue provider: {name!r}")
