from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError
from app.providers.queue.base import JobQueue


def build_job_queue(settings: Settings | None = None, jobs=None) -> JobQueue:
    """Construct the queue named by ``DREAM_QUEUE_PROVIDER``."""
    settings = settings or get_settings()
    name = settings.queue_provider

    if name == "arq":
        from app.providers.queue.arq_queue import ArqJobQueue

        return ArqJobQueue(settings.redis_url)

    if name == "mongo":
        from app.providers.queue.mongo_queue import MongoJobQueue
        from app.repositories.job_repository import JobRepository
        from app.repositories.mongo import get_database

        if jobs is None:
            jobs = JobRepository(get_database(settings))
        return MongoJobQueue(jobs)

    if name == "inline":
        from app.providers.queue.inline_queue import InlineJobQueue

        return InlineJobQueue()

    raise ConfigurationError(f"Unknown queue provider: {name!r}")
