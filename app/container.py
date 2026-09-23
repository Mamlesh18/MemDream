"""Composition root.

Everything is constructed here, once, and injected downwards. No module reaches
for a provider on its own — which is what keeps `app.memory` and `app.dream`
free of any import from a concrete provider, and makes the whole framework
testable by passing fakes to `DreamContainer.create`.

Both entry points build the same container: the FastAPI app on startup, and the
optional worker process on boot. There is no second wiring path that could
drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.metrics import metrics
from app.core.policy import MemoryPolicy
from app.core.prompts import PromptLibrary
from app.dream.compaction import CompactionEngine
from app.dream.consolidation import ConsolidationEngine
from app.dream.engine import DreamEngine
from app.dream.scheduler import ConsolidationScheduler
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
from app.repositories.base import AuditRepository, Database, JobRepository, MemoryRepository
from app.repositories.factory import build_repositories

logger = get_logger(__name__)


@dataclass
class DreamContainer:
    settings: Settings

    database: Database
    memories: MemoryRepository
    audit: AuditRepository
    jobs: JobRepository
    api_keys: Any

    embeddings: EmbeddingProvider
    llm: LLMProvider
    vectors: VectorStore
    queue: JobQueue

    ranker: HybridRanker
    retriever: MemoryRetriever
    lifecycle: MemoryLifecycle
    policy: MemoryPolicy
    prompts: PromptLibrary
    extractor: MemoryExtractor

    dream_engine: DreamEngine
    synthesis: SynthesisEngine
    consolidation: ConsolidationEngine
    compaction: CompactionEngine
    memory_service: MemoryService
    dream_service: DreamService

    #: The nightly consolidation clock. Attached by the API lifespan, so a
    #: container built by a test or a script never starts a background task it
    #: did not ask for.
    scheduler: ConsolidationScheduler | None = None

    @classmethod
    async def create(
        cls,
        settings: Settings | None = None,
        *,
        database: Database | None = None,
        embeddings: EmbeddingProvider | None = None,
        llm: LLMProvider | None = None,
        vectors: VectorStore | None = None,
        queue: JobQueue | None = None,
        ensure_schema: bool = True,
    ) -> DreamContainer:
        """Build the whole object graph.

        Every provider can be overridden, which is how tests inject fakes without
        touching environment variables or monkeypatching factories.
        """
        settings = settings or get_settings()

        if settings.otel_enabled and metrics.enable_otel(settings.service_name):
            logger.info("otel_metrics_enabled")

        # One call covers both backends: `database` overrides the connection
        # (that is how tests inject mongomock), and the repositories that match
        # it are chosen from the connection itself rather than from config.
        repositories = await build_repositories(settings, database=database)
        database = repositories.database
        if ensure_schema:
            await database.ensure_schema()

        from app.core.auth import ApiKeyRegistry

        memories = repositories.memories
        audit = repositories.audit
        jobs = repositories.jobs

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
        # Resolved and validated here: a deployment missing a prompt should fail
        # while it is starting, not on the first user message.
        prompts = PromptLibrary.from_settings(settings)
        extractor = MemoryExtractor(
            llm, policy=policy, settings=settings, prompts=prompts
        )

        dream_engine = DreamEngine(
            llm=llm,
            embeddings=embeddings,
            repository=memories,
            retriever=retriever,
            lifecycle=lifecycle,
            audit=audit,
            settings=settings,
            prompts=prompts,
        )
        consolidation = ConsolidationEngine(memories, lifecycle, settings)
        compaction = CompactionEngine(
            memories, lifecycle, llm, embeddings, settings, prompts=prompts
        )
        synthesis = SynthesisEngine(
            llm=llm,
            embeddings=embeddings,
            repository=memories,
            lifecycle=lifecycle,
            audit=audit,
            settings=settings,
            prompts=prompts,
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
            consolidation=consolidation,
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
            api_keys=ApiKeyRegistry(settings, database),
            embeddings=embeddings,
            llm=llm,
            vectors=vectors,
            queue=queue,
            ranker=ranker,
            retriever=retriever,
            lifecycle=lifecycle,
            policy=policy,
            prompts=prompts,
            extractor=extractor,
            dream_engine=dream_engine,
            synthesis=synthesis,
            consolidation=consolidation,
            compaction=compaction,
            memory_service=memory_service,
            dream_service=dream_service,
        )

        # The embedded queue runs jobs through the service that owns it, so the
        # wiring can only be completed once both exist.
        from app.providers.queue.inline_queue import EmbeddedJobQueue

        if isinstance(queue, EmbeddedJobQueue):
            async def _runner(job_id: str) -> None:
                await container.dream_service.run_job(job_id)

            # The repository comes too, so a job rejected by backpressure or
            # abandoned at shutdown can be failed with a reason rather than
            # left looking in-flight until the stale reaper notices.
            queue.bind(_runner, jobs)

        logger.info(
            "container_ready",
            database_provider=database.name,
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
            checks["database"] = {"ok": True, "provider": self.database.name}
        except Exception as exc:  # noqa: BLE001
            checks["database"] = {"ok": False, "provider": self.database.name, "error": str(exc)[:200]}

        try:
            checks["vector_store"] = {
                "ok": await self.vectors.health_check(),
                "provider": self.vectors.name,
            }
        except Exception as exc:  # noqa: BLE001
            checks["vector_store"] = {"ok": False, "error": str(exc)[:200]}

        try:
            checks["queue"] = {"ok": await self.queue.health_check(), "provider": self.queue.name}
            depth = getattr(self.queue, "depth", None)
            if depth is not None:
                # Waiting + running. A queue that is saturated is working hard
                # rather than broken, so it shows up here rather than as `ok: false`.
                checks["queue"]["depth"] = depth
        except Exception as exc:  # noqa: BLE001
            checks["queue"] = {"ok": False, "error": str(exc)[:200]}

        # Which prompts are in force and where each came from. The first thing
        # to check when a deployment behaves unlike the one next to it.
        checks["prompts"] = {"ok": True, "sources": self.prompts.describe()}
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
        """Release every held resource, continuing past individual failures.

        **Order matters.** The queue goes first because closing it drains
        in-flight synthesis, and a draining job still needs the database and the
        LLM client. Closing the database first would fail every job that was
        about to finish.
        """
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
