"""The job queue contract.

Kept deliberately thin — enqueue, health, close — so that adding Celery, RQ,
Temporal, SQS, Service Bus or Kafka is an adapter, not a refactor. Two
implementations ship: an in-process pool (the default) and the canonical
store's own jobs table. Neither needs a broker.
Job *state* is not part of this interface: it lives in Mongo, so status queries
never depend on which broker is installed, and a job survives the queue being
flushed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.dream import DreamJob


class JobQueue(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for logs and health output."""

    @abstractmethod
    async def enqueue_synthesis(self, job: DreamJob) -> str:
        """Queue a synthesis run and return the job id.

        Implementations must not block on execution: the caller is usually an
        HTTP request that has to return `202`-style promptly.
        """

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        return None
