"""Shared test fixtures.

Tests run fully offline against fakes: `mongomock-motor` for Mongo, the
in-process vector store, and the deterministic fake LLM and embedding providers.
No network, no containers, no API keys, no cost.

Set ``DREAM_TEST_MONGODB_URI`` to run the same suite against a real MongoDB.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("DREAM_IDENTITY_SALT", "test-salt-do-not-use-in-production")
os.environ.setdefault("DREAM_LOG_LEVEL", "WARNING")
os.environ.setdefault("DREAM_CONFIG_FILE", "")

from app.container import DreamContainer  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.identity import build_scope  # noqa: E402
from app.core.metrics import metrics  # noqa: E402
from app.models.dream import ExtractedMemory  # noqa: E402
from app.models.memory import Memory  # noqa: E402
from app.providers.embeddings.fake import FakeEmbeddingProvider  # noqa: E402
from app.providers.llm.fake import FakeLLMProvider  # noqa: E402
from app.providers.queue.inline_queue import InlineJobQueue  # noqa: E402
from app.providers.vector_store.memory_store import InMemoryVectorStore  # noqa: E402
from app.repositories.mongo import MongoDatabase  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    """Test settings with every threshold made explicit.

    Values are stated here rather than inherited from defaults so that a test
    failing after a default changes points at the default, not at the test.
    """
    return Settings(
        env="test",
        identity_salt="test-salt-do-not-use-in-production",
        # MemDream ships no prompts, so the suite supplies the starter set the
        # repository ships as templates — the same files a company copies.
        prompts_dir=str(Path(__file__).resolve().parents[1] / "prompts"),
        llm_provider="fake",
        embedding_provider="fake",
        vector_provider="memory",
        queue_provider="inline",
        mongodb_database="memdream_test",
        log_level="WARNING",
        retrieval={
            "top_k": 10,
            "similarity_threshold": 0.15,
            "candidate_multiplier": 4,
        },
        memory={
            "min_confidence": 0.5,
            "min_importance": 0.1,
            "allow_sensitive": False,
        },
        dream={
            "decision_mode": "unified",
            "supersede": {"enabled": True, "similarity_threshold": 0.05},
            "merge": {"enabled": True, "similarity_threshold": 0.15},
            "synthesis": {
                "enabled": True,
                "min_memories": 4,
                "min_cluster_size": 3,
                "cluster_similarity_threshold": 0.15,
                "min_pattern_confidence": 0.5,
                "min_pattern_importance": 0.3,
            },
        },
    )


@pytest.fixture
async def database() -> MongoDatabase:
    """A MongoDatabase backed by mongomock, or a real server when configured."""
    uri = os.environ.get("DREAM_TEST_MONGODB_URI")
    if uri:
        from motor.motor_asyncio import AsyncIOMotorClient

        client = AsyncIOMotorClient(uri, tz_aware=True)
        db = MongoDatabase(client, "memdream_test")
        await db.drop_everything()
        yield db
        await db.drop_everything()
        await db.close()
        return

    from mongomock_motor import AsyncMongoMockClient

    client = AsyncMongoMockClient(tz_aware=True)
    yield MongoDatabase(client, "memdream_test")


@pytest.fixture
def llm() -> FakeLLMProvider:
    return FakeLLMProvider()


@pytest.fixture
def embeddings() -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider()


@pytest.fixture
async def container(settings, database, llm, embeddings) -> DreamContainer:
    """A fully wired container using fakes throughout."""
    metrics.reset()
    built = await DreamContainer.create(
        settings,
        database=database,
        llm=llm,
        embeddings=embeddings,
        vectors=InMemoryVectorStore(),
        queue=InlineJobQueue(),
        # mongomock's index support is partial and the suite does not depend on
        # index-backed performance.
        ensure_schema=bool(os.environ.get("DREAM_TEST_MONGODB_URI")),
    )
    yield built
    await built.vectors.close()


@pytest.fixture
def scope(settings):
    return build_scope(
        client_id="client_test",
        bot_id="bot_test",
        phone_number="+919999999999",
        settings=settings,
    )


@pytest.fixture
def other_scope(settings):
    """A different client, same phone number — the tenant-isolation control."""
    return build_scope(
        client_id="client_other",
        bot_id="bot_test",
        phone_number="+919999999999",
        settings=settings,
    )


@pytest.fixture
def make_memory(scope):
    """Build and persist-ready a `Memory` without going through extraction."""

    def _make(
        content: str,
        *,
        memory_type: str = "fact",
        importance: float = 0.5,
        confidence: float = 0.9,
        target_scope=None,
        **kwargs,
    ) -> Memory:
        used = target_scope or scope
        return Memory(
            **used.as_model_fields(),
            content=content,
            memory_type=memory_type,
            importance=importance,
            confidence=confidence,
            **kwargs,
        )

    return _make


@pytest.fixture
def store_memory(container, make_memory, embeddings):
    """Persist a memory and index it, bypassing the Dream engine."""

    async def _store(content: str, *, target_scope=None, **kwargs) -> Memory:
        from app.models.scope import MemoryScope

        used: MemoryScope = target_scope or kwargs.pop("scope", None)
        memory = make_memory(content, target_scope=used, **kwargs)
        memory.embedding = await embeddings.embed(content)
        memory.embedding_model = embeddings.model_name
        memory.embedding_dim = embeddings.dimensions

        from app.models.scope import MemoryScope as _S

        active_scope = used or _S(
            client_id=memory.client_id, bot_id=memory.bot_id, user_id=memory.user_id
        )
        await container.lifecycle.activate(active_scope, memory)
        return memory

    return _store


@pytest.fixture
def candidate():
    """Build an `ExtractedMemory` directly, skipping the extraction LLM call."""

    def _candidate(
        content: str,
        *,
        memory_type: str = "fact",
        importance: float = 0.6,
        confidence: float = 0.95,
        **kwargs,
    ) -> ExtractedMemory:
        return ExtractedMemory(
            content=content,
            memory_type=memory_type,
            importance=importance,
            confidence=confidence,
            **kwargs,
        )

    return _candidate
