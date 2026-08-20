"""Composition root.

Everything is constructed here, once, and injected downwards. No module reaches
for a provider on its own — which is what keeps `app.memory` and `app.dream`
free of any import from a concrete provider, and makes the whole framework
testable by passing fakes to `DreamContainer.create`.

Both entry points build the same container: the FastAPI app on startup, and the
ARQ worker on boot. There is no second wiring path that could drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.metrics import metrics
from app.core.policy import MemoryPolicy
from app.dream.engine import DreamEngine
from app.dream.service import DreamService
from app.dream.synthesis import SynthesisEngine
from app.memory.extractor import MemoryExtractor
from app.memory.lifecycle import MemoryLifecycle
from app.memory.ranker import HybridRanker
from app.memory.retriever import MemoryRetriever
from app.memory.service import MemoryService
from app.providers.embeddings.base import EmbeddingProvider
from app.providers.embeddings.factory import build_embedding_provider
from app.providers.llm.base import LLMProvider
from app.providers.llm.factory import build_llm_provider
from app.providers.queue.base import JobQueue
from app.providers.queue.factory import build_job_queue
from app.providers.vector_store.base import VectorStore
from app.providers.vector_store.factory import build_vector_store
from app.repositories.audit_repository import AuditRepository
from app.repositories.job_repository import JobRepository
from app.repositories.memory_repository import MemoryRepository
from app.repositories.mongo import MongoDatabase

logger = get_logger(__name__)


@dataclass
class DreamContainer:
    settings: Settings

    database: MongoDatabase
    memories: MemoryRepository
    audit: AuditRepository
    jobs: JobRepository

    embeddings: EmbeddingProvider
    llm: LLMProvider
    vectors: VectorStore
    queue: JobQueue

    ranker: HybridRanker
    retriever: MemoryRetriever
    lifecycle: MemoryLifecycle
    policy: MemoryPolicy
    extractor: MemoryExtractor

    dream_engine: DreamEngine
    synthesis: SynthesisEngine
    memory_service: MemoryService
    dream_service: DreamService

    @classmethod
    async def create(
        cls,
        settings: Settings | None = None,
        *,
        database: MongoDatabase | None = None,
        embeddings: EmbeddingProvider | None = None,
        llm: LLMProvider | None = None,
        vectors: VectorStore | None = None,
        queue: JobQueue | None = None,
        ensure_indexes: bool = True,
    ) -> DreamContainer:
        """Build the whole object graph.

        Every provider can be overridden, which is how tests inject fakes without
        touching environment variables or monkeypatching factories.
        """
        settings = settings or get_settings()

        if settings.otel_enabled and metrics.enable_otel(settings.service_name):
            logger.info("otel_metrics_enabled")

        database = database or MongoDatabase.connect(settings)
        if ensure_indexes:
            await database.ensure_indexes()

        memories = MemoryRepository(database)
        audit = AuditRepository(database)
        jobs = JobRepository(database)

        embeddings = embeddings or build_embedding_provider(settings)
        llm = llm or build_llm_provider(settings)
        # The index width comes from the live provider, not from configuration,
        # so an index is always created at the dimension the model returns.
        vectors = vectors or build_vector_store(settings, dimensions=embeddings.dimensions)
        queue = queue or build_job_queue(settings, jobs=jobs)

        ranker = HybridRanker(settings.retrieval)
        retriever = MemoryRetriever(
            memories, vectors, embeddings, settings=settings, ranker=ranker
        )
        lifecycle = MemoryLifecycle(memories, retriever)
        policy = MemoryPolicy(settings.memory)
        extractor = MemoryExtractor(llm, policy=policy, settings=settings)

        dream_engine = DreamEngine(
            llm=llm,
            embeddings=embeddings,
            repository=memories,
            retriever=retriever,
            lifecycle=lifecycle,
            audit=audit,
            settings=settings,
        )
        synthesis = SynthesisEngine(
            llm=llm,
            embeddings=embeddings,
            repository=memories,
            lifecycle=lifecycle,
            audit=audit,
            settings=settings,
        )
        memory_service = MemoryService(
            repository=memories,
            retriever=retriever,
            lifecycle=lifecycle,
            extractor=extractor,
            engine=dream_engine,
            audit=audit,
            embeddings=embeddings,
            settings=settings,
        )
        dream_service = DreamService(
            synthesis=synthesis,
            jobs=jobs,
            memories=memories,
            queue=queue,
            settings=settings,
        )

        container = cls(
            settings=settings,
            database=database,
            memories=memories,
            audit=audit,
            jobs=jobs,
            embeddings=embeddings,
            llm=llm,
            vectors=vectors,
            queue=queue,
            ranker=ranker,
            retriever=retriever,
            lifecycle=lifecycle,
            policy=policy,
            extractor=extractor,
            dream_engine=dream_engine,
            synthesis=synthesis,
            memory_service=memory_service,
            dream_service=dream_service,
        )

        # The inline queue runs jobs through the service that owns it, so the
        # wiring can only be completed once both exist.
        from app.providers.queue.inline_queue import InlineJobQueue

        if isinstance(queue, InlineJobQueue):
            async def _runner(job_id: str, client_id: str, bot_id: str, user_id: str) -> None:
                await container.dream_service.run_job(job_id)

            queue.set_runner(_runner)

        logger.info(
            "container_ready",
            llm_provider=settings.llm_provider,
            embedding_provider=settings.embedding_provider,
            vector_provider=vectors.name,
            queue_provider=queue.name,
            embedding_dimensions=embeddings.dimensions,
            decision_mode=settings.dream.decision_mode,
        )
        return container

    # --- lifecycle ------------------------------------------------------------- #

    async def health(self) -> dict[str, Any]:
        """Per-dependency health. Never raises — a health endpoint that 500s
        because a dependency is down tells you less than one that reports it."""
        checks: dict[str, Any] = {}

        try:
            await self.database.ping()
            checks["mongodb"] = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            checks["mongodb"] = {"ok": False, "error": str(exc)[:200]}

        try:
            checks["vector_store"] = {
                "ok": await self.vectors.health_check(),
                "provider": self.vectors.name,
            }
        except Exception as exc:  # noqa: BLE001
            checks["vector_store"] = {"ok": False, "error": str(exc)[:200]}

        try:
            checks["queue"] = {"ok": await self.queue.health_check(), "provider": self.queue.name}
        except Exception as exc:  # noqa: BLE001
            checks["queue"] = {"ok": False, "error": str(exc)[:200]}

        checks["embeddings"] = {
            "provider": self.settings.embedding_provider,
            "model": self.embeddings.model_name,
            "dimensions": self.embeddings.dimensions,
        }
        checks["llm"] = {
            "provider": self.settings.llm_provider,
            "model": self.llm.model_name,
        }
        return checks

    async def close(self) -> None:
        """Release every held resource, continuing past individual failures."""
        for name, closer in (
            ("queue", self.queue.close),
            ("vectors", self.vectors.close),
            ("llm", self.llm.close),
            ("embeddings", self.embeddings.close),
            ("database", self.database.close),
        ):
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001
                logger.warning("container_close_failed", component=name, error=str(exc)[:200])
