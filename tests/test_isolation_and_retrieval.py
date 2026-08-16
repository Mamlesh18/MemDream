"""Tenant isolation, identity derivation, ranking, policy and the vector store."""

from __future__ import annotations

import tempfile

import pytest

from app.core.config import MemoryPolicySettings, RetrievalSettings
from app.core.exceptions import TenantIsolationError, ValidationError
from app.core.identity import build_scope, derive_user_id, hash_phone_number, normalize_phone_number
from app.core.policy import MemoryPolicy
from app.memory.ranker import HybridRanker
from app.models.dream import ExtractedMemory
from app.models.enums import MemoryStatus
from app.models.memory import Memory, utcnow
from app.models.scope import MemoryScope
from app.providers.vector_store.base import VectorRecord
from app.providers.vector_store.faiss_store import FaissVectorStore

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+919999999999", "+919999999999"),
        ("00919999999999", "+919999999999"),
        ("+91 99999 99999", "+919999999999"),
        ("+91-99999-99999", "+919999999999"),
        ("  +919999999999  ", "+919999999999"),
        ("919999999999", "+919999999999"),
    ],
)
async def test_phone_numbers_normalise_to_one_form(raw, expected):
    assert normalize_phone_number(raw) == expected


@pytest.mark.parametrize("bad", ["", "   ", "abc", "+", "12", "+0123456789"])
async def test_invalid_phone_numbers_are_rejected(bad):
    with pytest.raises(ValidationError):
        normalize_phone_number(bad)


async def test_user_id_is_scoped_to_client_and_bot(settings):
    """The same person contacting two clients gets two unrelated identifiers, so
    identity cannot be correlated across tenants by comparing stored ids."""
    base = {"phone_number": "+919999999999", "settings": settings}

    a = derive_user_id(client_id="c1", bot_id="b1", **base)
    b = derive_user_id(client_id="c2", bot_id="b1", **base)
    c = derive_user_id(client_id="c1", bot_id="b2", **base)

    assert a != b != c
    assert a != c
    assert derive_user_id(client_id="c1", bot_id="b1", **base) == a, "must be deterministic"


async def test_raw_phone_number_never_appears_in_the_identifiers(settings):
    scope = build_scope(
        client_id="c1", bot_id="b1", phone_number="+919999999999", settings=settings
    )
    assert "9999999999" not in scope.user_id
    assert "9999999999" not in (scope.phone_number_hash or "")


async def test_a_different_salt_produces_different_identities(settings):
    from app.core.config import Settings

    other = Settings(identity_salt="a-completely-different-salt", env="test")
    assert hash_phone_number("+919999999999", settings) != hash_phone_number("+919999999999", other)


async def test_explicit_user_id_wins_over_derivation(settings):
    scope = build_scope(
        client_id="c1",
        bot_id="b1",
        phone_number="+919999999999",
        user_id="caller_supplied_id",
        settings=settings,
    )
    assert scope.user_id == "caller_supplied_id"
    # The phone number is still hashed, for reconciliation.
    assert scope.phone_number_hash


async def test_identity_requires_a_phone_number_or_a_user_id(settings):
    with pytest.raises(ValidationError):
        build_scope(client_id="c1", bot_id="b1", settings=settings)


# --------------------------------------------------------------------------- #
# Tenant isolation
# --------------------------------------------------------------------------- #


async def test_repository_refuses_a_document_from_another_scope(container, scope, other_scope):
    """Defence in depth: the query filter already prevents this, so reaching the
    guard means a bug — and it should be loud, not a silent wrong answer."""
    foreign = Memory(
        client_id=other_scope.client_id,
        bot_id=other_scope.bot_id,
        user_id=other_scope.user_id,
        content="Another tenant's memory.",
    )
    with pytest.raises(TenantIsolationError):
        container.memories._guard(scope, foreign.to_document())


async def test_bots_within_one_client_are_isolated(container, settings, store_memory):
    bot_a = build_scope(client_id="acme", bot_id="support", phone_number="+919999999999", settings=settings)
    bot_b = build_scope(client_id="acme", bot_id="sales", phone_number="+919999999999", settings=settings)

    await store_memory("User raised a support ticket.", target_scope=bot_a)
    await store_memory("User asked about pricing.", target_scope=bot_b)

    a_memories = await container.memories.find(bot_a, limit=50)
    b_memories = await container.memories.find(bot_b, limit=50)

    assert len(a_memories) == 1
    assert len(b_memories) == 1
    assert a_memories[0].memory_id != b_memories[0].memory_id


async def test_eligible_ids_never_leak_across_scopes(container, scope, other_scope, store_memory):
    await store_memory("A memory for the first tenant.", target_scope=scope)
    await store_memory("A memory for the second tenant.", target_scope=other_scope)

    ids = await container.memories.eligible_ids(scope, statuses=[MemoryStatus.ACTIVE])
    other_ids = await container.memories.eligible_ids(other_scope, statuses=[MemoryStatus.ACTIVE])

    assert ids and other_ids
    assert ids.isdisjoint(other_ids)


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #


def _memory(content="A memory.", **kwargs) -> Memory:
    return Memory(client_id="c", bot_id="b", user_id="u", content=content, **kwargs)


async def test_ranking_demotes_superseded_memories_below_identical_active_ones():
    """The reason `status_multipliers` exists: with the additive term alone, a
    retired memory that phrases things closer to the query wins."""
    ranker = HybridRanker(RetrievalSettings())
    active = _memory("User lives in Bangalore.")
    superseded = _memory("User lives in Chennai.", status=MemoryStatus.SUPERSEDED)

    # Identical inputs except status, and the retired one is *more* similar.
    ranked = ranker.rank([(active, 0.60), (superseded, 0.75)])

    assert ranked[0].memory.status == MemoryStatus.ACTIVE


async def test_importance_can_outrank_a_closer_match():
    ranker = HybridRanker(RetrievalSettings())
    trivial = _memory("User likes coffee.", importance=0.2, confidence=0.7)
    critical = _memory("User's production database is PostgreSQL.", importance=0.95, confidence=0.99)

    ranked = ranker.rank([(trivial, 0.72), (critical, 0.62)])

    assert ranked[0].memory is critical


async def test_scores_stay_within_range_and_expose_their_components():
    ranker = HybridRanker(RetrievalSettings())
    scored = ranker.score(_memory(importance=1.0, confidence=1.0), 1.0)

    assert 0.0 <= scored.score <= 1.0
    assert scored.semantic_similarity == 1.0
    assert 0.0 <= scored.recency_score <= 1.0


async def test_negative_similarity_cannot_drag_a_score_below_zero():
    ranker = HybridRanker(RetrievalSettings())
    scored = ranker.score(_memory(), -0.4)
    assert scored.score >= 0.0


async def test_recency_decays_with_age():
    from datetime import timedelta

    ranker = HybridRanker(RetrievalSettings(recency_half_life_days=30.0))
    now = utcnow()

    fresh = _memory(updated_at=now)
    old = _memory(updated_at=now - timedelta(days=30))

    assert ranker.recency_score(fresh, now) > ranker.recency_score(old, now)
    assert ranker.recency_score(old, now) == pytest.approx(0.5, abs=0.01)


async def test_ranking_is_stable_across_runs():
    ranker = HybridRanker(RetrievalSettings())
    pairs = [(_memory(f"Memory {i}"), 0.5) for i in range(10)]

    first = [s.memory.memory_id for s in ranker.rank(pairs)]
    second = [s.memory.memory_id for s in ranker.rank(pairs)]

    assert first == second


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


def _candidate(**kwargs) -> ExtractedMemory:
    return ExtractedMemory(**{"content": "User likes coffee.", "memory_type": "preference", **kwargs})


async def test_policy_rejects_low_confidence_and_importance():
    policy = MemoryPolicy(MemoryPolicySettings(min_confidence=0.7, min_importance=0.3))

    assert not policy.evaluate(_candidate(confidence=0.5, importance=0.9)).accepted
    assert not policy.evaluate(_candidate(confidence=0.9, importance=0.1)).accepted
    assert policy.evaluate(_candidate(confidence=0.9, importance=0.5)).accepted


async def test_policy_blocks_sensitive_candidates_by_default():
    strict = MemoryPolicy(MemoryPolicySettings(allow_sensitive=False))
    permissive = MemoryPolicy(MemoryPolicySettings(allow_sensitive=True))
    candidate = _candidate(sensitive=True, confidence=0.99, importance=0.9)

    decision = strict.evaluate(candidate)
    assert not decision.accepted
    assert "sensitive" in decision.reason
    assert permissive.evaluate(candidate).accepted


async def test_policy_honours_allow_and_block_lists():
    blocked = MemoryPolicy(MemoryPolicySettings(blocked_memory_types=["preference"]))
    assert not blocked.evaluate(_candidate(memory_type="preference")).accepted
    assert blocked.evaluate(_candidate(memory_type="fact")).accepted

    allowed = MemoryPolicy(MemoryPolicySettings(allowed_memory_types=["fact"]))
    assert not allowed.evaluate(_candidate(memory_type="preference")).accepted
    assert allowed.evaluate(_candidate(memory_type="fact")).accepted


async def test_empty_allow_list_means_no_restriction():
    policy = MemoryPolicy(MemoryPolicySettings(allowed_memory_types=[]))
    assert policy.evaluate(_candidate(memory_type="anything_at_all")).accepted


async def test_policy_caps_candidates_per_prompt_keeping_the_important_ones():
    policy = MemoryPolicy(MemoryPolicySettings(max_candidates_per_prompt=2, min_importance=0.0))
    candidates = [_candidate(content=f"Memory {i}.", importance=i / 10) for i in range(6)]

    accepted, rejected = policy.filter(candidates)

    assert len(accepted) == 2
    assert len(rejected) == 4
    assert min(c.importance for c in accepted) > max(c.importance for c, _ in rejected)


async def test_policy_rejects_overlong_content():
    policy = MemoryPolicy(MemoryPolicySettings(max_content_chars=50))
    assert not policy.evaluate(_candidate(content="x" * 100)).accepted


# --------------------------------------------------------------------------- #
# FAISS vector store
# --------------------------------------------------------------------------- #


@pytest.fixture
def faiss_store():
    with tempfile.TemporaryDirectory() as directory:
        yield FaissVectorStore(path=directory, dimensions=8)


def _scope(client="c1", bot="b1", user="u1") -> MemoryScope:
    return MemoryScope(client_id=client, bot_id=bot, user_id=user)


async def test_faiss_partitions_are_physically_separate(faiss_store, embeddings):
    a, b = _scope(client="tenant_a"), _scope(client="tenant_b")
    vector = await embeddings.embed("A shared sentence.")

    await faiss_store.upsert(a, [VectorRecord("mem_a", vector)])
    await faiss_store.upsert(b, [VectorRecord("mem_b", vector)])

    assert await faiss_store.count(a) == 1
    assert await faiss_store.count(b) == 1

    hits = await faiss_store.search(a, vector, top_k=10)
    assert [hit.memory_id for hit in hits] == ["mem_a"]


async def test_faiss_upsert_replaces_rather_than_duplicates(faiss_store, embeddings):
    scope = _scope()
    first = await embeddings.embed("The original text.")
    second = await embeddings.embed("A completely different sentence.")

    await faiss_store.upsert(scope, [VectorRecord("mem_1", first)])
    await faiss_store.upsert(scope, [VectorRecord("mem_1", second)])

    assert await faiss_store.count(scope) == 1
    hits = await faiss_store.search(scope, second, top_k=5)
    assert hits[0].memory_id == "mem_1"
    assert hits[0].score > 0.9


async def test_faiss_delete_removes_from_the_index(faiss_store, embeddings):
    scope = _scope()
    vector = await embeddings.embed("Something to forget from the index.")

    await faiss_store.upsert(scope, [VectorRecord("mem_1", vector)])
    await faiss_store.delete(scope, ["mem_1"])

    assert await faiss_store.count(scope) == 0
    assert await faiss_store.search(scope, vector, top_k=5) == []


async def test_faiss_allow_list_is_applied(faiss_store, embeddings):
    scope = _scope()
    for index, text in enumerate(["alpha beta", "beta gamma", "gamma delta"]):
        await faiss_store.upsert(scope, [VectorRecord(f"mem_{index}", await embeddings.embed(text))])

    query = await embeddings.embed("beta")
    hits = await faiss_store.search(scope, query, top_k=10, allowed_ids={"mem_2"})

    assert {hit.memory_id for hit in hits} <= {"mem_2"}


async def test_faiss_empty_allow_list_returns_nothing(faiss_store, embeddings):
    """An empty pre-filter means nothing was eligible. It must not be mistaken
    for 'no filter'."""
    scope = _scope()
    vector = await embeddings.embed("Indexed content.")
    await faiss_store.upsert(scope, [VectorRecord("mem_1", vector)])

    assert await faiss_store.search(scope, vector, top_k=5, allowed_ids=set()) == []


async def test_faiss_survives_a_reload_from_disk(faiss_store, embeddings):
    scope = _scope()
    vector = await embeddings.embed("Persisted across a restart.")
    await faiss_store.upsert(scope, [VectorRecord("mem_1", vector)])

    # Simulate a fresh process pointed at the same directory.
    reopened = FaissVectorStore(path=str(faiss_store._root), dimensions=8)
    hits = await reopened.search(scope, vector, top_k=5)

    assert [hit.memory_id for hit in hits] == ["mem_1"]


async def test_faiss_reports_cosine_similarity(faiss_store, embeddings):
    scope = _scope()
    vector = await embeddings.embed("User likes black coffee.")
    await faiss_store.upsert(scope, [VectorRecord("mem_1", vector)])

    hits = await faiss_store.search(scope, vector, top_k=1)
    assert hits[0].score == pytest.approx(1.0, abs=1e-4)
