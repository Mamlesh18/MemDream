"""Phase 4 — folding crowds of low-value memories into single summaries.

This is the riskiest thing in consolidation and the only part that asks a model
to compress facts. Everything else retires memories that are provably false or
demonstrably unused; this one *rewrites* several into one, and a compression can
drop something.

So the tests split in two. A handful check that it works. The rest check what it
refuses to touch — because the way this feature fails is not by throwing, it is
by quietly folding a peanut allergy into a paragraph about lunch orders.

The offline fake summarises by concatenation, which is a deliberately terrible
summary that nonetheless satisfies the contract: every input survives verbatim.
A fake that produced a *plausible* summary while dropping a fact would hide the
regression these tests exist to catch.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.dream.compaction import CompactionEngine
from app.models.enums import MemoryStatus, MemoryType, TemporalNature
from app.models.memory import Memory, utcnow

pytestmark = pytest.mark.asyncio

OLD = utcnow() - timedelta(days=200)


@pytest.fixture
def compactor(container):
    config = container.settings.dream.compaction
    config.enabled = True
    config.min_memories = 4
    config.min_cluster_size = 3
    config.similarity_threshold = 0.15  # the offline embeddings score low
    return CompactionEngine(
        container.memories, container.lifecycle, container.llm,
        container.embeddings, container.settings,
    )


async def _store(container, scope, content, **overrides):
    overrides.setdefault("created_at", OLD)
    overrides.setdefault("importance", 0.25)
    memory = Memory(
        **scope.as_model_fields(),
        content=content,
        embedding=await container.embeddings.embed(content),
        embedding_model=container.embeddings.model_name,
        **overrides,
    )
    await container.lifecycle.activate(scope, memory)
    return memory


async def _crowd(container, scope, n=6):
    return [
        await _store(container, scope, f"User ordered a masala dosa on day {i}.")
        for i in range(n)
    ]


# --------------------------------------------------------------------------- #
# It folds
# --------------------------------------------------------------------------- #


async def test_a_crowd_becomes_one_memory(compactor, container, scope):
    crowd = await _crowd(container, scope)

    outcome = await compactor.run(scope)

    assert outcome.compacted, "nothing was folded"
    assert outcome.absorbed >= 3
    active, _ = await container.memory_service.list_memories(
        scope, statuses=[MemoryStatus.ACTIVE], limit=50
    )
    assert len(active) < len(crowd), "the active set should have shrunk"


async def test_the_originals_are_merged_not_deleted(compactor, container, scope):
    """The property the whole feature rests on. A summary that turns out to be
    bad is recoverable only because its inputs are still there."""
    crowd = await _crowd(container, scope)

    await compactor.run(scope)

    for memory in crowd:
        stored = await container.memories.get(scope, memory.memory_id)
        assert stored is not None, "an input was deleted"
        if stored.status is MemoryStatus.MERGED:
            assert stored.merged_into, "a folded memory must point at its replacement"
            assert stored.content == memory.content, "and keep its own words"


async def test_the_summary_carries_its_sources(compactor, container, scope):
    await _crowd(container, scope)

    outcome = await compactor.run(scope)
    summary = await container.memories.get(scope, outcome.compacted[0])

    assert len(summary.source_memory_ids) >= 3
    assert summary.metadata.get("source") == "compaction"


async def test_a_summary_is_inferred_not_testimony(compactor, container, scope):
    """Every fact in it was stated, but the user never said this sentence."""
    await _crowd(container, scope)

    outcome = await compactor.run(scope)
    summary = await container.memories.get(scope, outcome.compacted[0])

    assert summary.explicit is False


async def test_folded_memories_leave_the_search_index(compactor, container, scope):
    """Otherwise the same fact comes back twice under two ids."""
    from app.models.requests import SearchMemoryRequest

    await _crowd(container, scope)
    await compactor.run(scope)

    results = await container.memory_service.search(
        scope, SearchMemoryRequest(user_id=scope.user_id, query="masala dosa", limit=20)
    )
    assert all(r.memory.status is not MemoryStatus.MERGED for r in results)


# --------------------------------------------------------------------------- #
# What it refuses to touch
# --------------------------------------------------------------------------- #


async def test_important_memories_never_reach_the_model(compactor, container, scope):
    """An allergy folded into a paragraph about lunch orders is the failure this
    feature would be remembered for."""
    allergy = await _store(
        container, scope, "User is allergic to peanuts.", importance=0.95,
    )
    await _crowd(container, scope)

    await compactor.run(scope)

    stored = await container.memories.get(scope, allergy.memory_id)
    assert stored.status is MemoryStatus.ACTIVE
    assert stored.content == "User is allergic to peanuts."


async def test_permanent_facts_are_never_folded(compactor, container, scope):
    born = await _store(
        container, scope, "User was born in Chennai.",
        temporal_nature=TemporalNature.PERMANENT,
    )
    await _crowd(container, scope)

    await compactor.run(scope)

    assert (
        await container.memories.get(scope, born.memory_id)
    ).status is MemoryStatus.ACTIVE


async def test_pattern_evidence_is_never_folded(compactor, container, scope):
    """Fold the evidence and the pattern's sources point at a memory that is no
    longer active — the citation survives but the trail goes cold."""
    evidence = [
        await _store(container, scope, f"User ran before work on day {i}.")
        for i in range(4)
    ]
    await _store(
        container, scope, "User exercises most mornings.",
        memory_type=MemoryType.PATTERN.value, explicit=False, importance=0.6,
        source_memory_ids=[m.memory_id for m in evidence],
    )
    await _crowd(container, scope)

    await compactor.run(scope)

    for memory in evidence:
        assert (
            await container.memories.get(scope, memory.memory_id)
        ).status is MemoryStatus.ACTIVE


async def test_recent_memories_are_left_alone(compactor, container, scope):
    """Compacting something said yesterday is compressing the conversation you
    are in."""
    fresh = await _store(
        container, scope, "User ordered a masala dosa today.", created_at=utcnow()
    )
    await _crowd(container, scope)

    await compactor.run(scope)

    assert (
        await container.memories.get(scope, fresh.memory_id)
    ).status is MemoryStatus.ACTIVE


async def test_patterns_themselves_are_never_folded(compactor, container, scope):
    pattern = await _store(
        container, scope, "User exercises most mornings.",
        memory_type=MemoryType.PATTERN.value, explicit=False,
        source_memory_ids=["mem_whatever"],
    )
    await _crowd(container, scope)

    await compactor.run(scope)

    assert (
        await container.memories.get(scope, pattern.memory_id)
    ).status is MemoryStatus.ACTIVE


# --------------------------------------------------------------------------- #
# Cost and caution
# --------------------------------------------------------------------------- #


async def test_it_is_off_by_default(container):
    assert container.settings.dream.compaction.enabled is False


async def test_a_small_account_costs_no_model_call(container, scope, llm):
    """The gate that keeps a nightly pass over a thousand users affordable.
    Below the threshold there is not enough clutter to be worth a call."""
    config = container.settings.dream.compaction
    config.enabled = True
    config.min_memories = 40

    engine = CompactionEngine(
        container.memories, container.lifecycle, container.llm,
        container.embeddings, container.settings,
    )
    await _crowd(container, scope, n=5)
    before = len(llm.calls)

    outcome = await engine.run(scope)

    assert outcome.clusters == 0
    assert len(llm.calls) == before, "it must not have asked the model anything"


async def test_a_dry_run_changes_nothing(compactor, container, scope):
    crowd = await _crowd(container, scope)

    outcome = await compactor.run(scope, dry_run=True)

    assert outcome.absorbed >= 3, "it still reports what it would fold"
    for memory in crowd:
        assert (
            await container.memories.get(scope, memory.memory_id)
        ).status is MemoryStatus.ACTIVE


async def test_a_suspiciously_short_summary_is_refused(compactor, container, scope, monkeypatch):
    """A summary shorter than the longest thing it replaces has almost certainly
    dropped something. Cheap check, and it catches the failure this feature is
    most likely to have."""
    from app.models.dream import CompactionResult
    from app.providers.llm.base import LLMResult

    async def terse(**kwargs):
        return LLMResult(
            data=CompactionResult(keep=True, summary="Food.", reason="lossy"),
            model="fake", latency_ms=1.0, usage={}, raw="",
        )

    monkeypatch.setattr(container.llm, "generate_json", terse)
    crowd = await _crowd(container, scope)

    outcome = await compactor.run(scope)

    assert outcome.compacted == []
    assert outcome.rejected >= 1
    for memory in crowd:
        assert (
            await container.memories.get(scope, memory.memory_id)
        ).status is MemoryStatus.ACTIVE


async def test_a_model_refusal_folds_nothing(compactor, container, scope, monkeypatch):
    """`keep: false` means these do not belong together, and storing them apart
    is the right answer."""
    from app.models.dream import CompactionResult
    from app.providers.llm.base import LLMResult

    async def refuse(**kwargs):
        return LLMResult(
            data=CompactionResult(keep=False, reason="unrelated"),
            model="fake", latency_ms=1.0, usage={}, raw="",
        )

    monkeypatch.setattr(container.llm, "generate_json", refuse)
    await _crowd(container, scope)

    outcome = await compactor.run(scope)

    assert outcome.compacted == []
    assert outcome.rejected >= 1


async def test_a_provider_failure_folds_nothing(compactor, container, scope, monkeypatch):
    from app.core.exceptions import ProviderError

    async def boom(**kwargs):
        raise ProviderError("the model is down")

    monkeypatch.setattr(container.llm, "generate_json", boom)
    crowd = await _crowd(container, scope)

    outcome = await compactor.run(scope)

    assert outcome.compacted == []
    for memory in crowd:
        assert (
            await container.memories.get(scope, memory.memory_id)
        ).status is MemoryStatus.ACTIVE
