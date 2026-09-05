"""Forgetting, and the signal that will eventually drive it.

Two phases, deliberately separated.

**Phase 0 — recall tracking.** Collects only. Nothing reads it yet, because
decay thresholds set against a week of data would be guesses. The tests here
check that the signal is recorded accurately and, more importantly, that
recording it does not disturb anything else.

**Phase 1 — lapsed expiry.** The one rule that needs no model and no judgement:
a memory whose validity window has closed is not stale or unpopular, it is
*false*. Everything else about decay is deferred until there is data to set it
against.

The protection tests matter more than the expiry tests. The failure this design
exists to prevent is a peanut allergy quietly leaving the active set.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.dream.consolidation import ConsolidationEngine
from app.models.enums import MemoryStatus, MemoryType
from app.models.memory import Memory, utcnow

pytestmark = pytest.mark.asyncio


@pytest.fixture
def consolidator(container):
    return ConsolidationEngine(container.memories, container.lifecycle, container.settings)


async def _store(container, scope, content, **overrides):
    memory = Memory(
        **scope.as_model_fields(),
        content=content,
        embedding=await container.embeddings.embed(content),
        embedding_model=container.embeddings.model_name,
        **overrides,
    )
    await container.lifecycle.activate(scope, memory)
    return memory


# --------------------------------------------------------------------------- #
# Phase 0 — the recall signal
# --------------------------------------------------------------------------- #


async def test_a_search_records_what_it_returned(container, scope):
    await _store(container, scope, "User is allergic to peanuts.")

    from app.models.requests import SearchMemoryRequest

    results = await container.memory_service.search(
        scope, SearchMemoryRequest(user_id=scope.user_id, query="peanuts allergy")
    )
    assert results, "nothing came back, so there is nothing to assert about"

    stored = await container.memories.get(scope, results[0].memory.memory_id)
    assert stored.recall_count == 1
    assert stored.last_recalled_at is not None


async def test_recall_counts_accumulate(container, scope):
    memory = await _store(container, scope, "User is allergic to peanuts.")

    for _ in range(3):
        await container.memories.record_recall(scope, [memory.memory_id])

    assert (await container.memories.get(scope, memory.memory_id)).recall_count == 3


async def test_recall_does_not_count_as_an_update(container, scope):
    """The subtle one. If a read bumped `updatedAt`, the ranker's recency term
    would feed back into itself — every search would make its own results look
    fresher, and whatever was already popular would climb forever."""
    memory = await _store(container, scope, "User cycles to work.")
    before = (await container.memories.get(scope, memory.memory_id)).updated_at

    await container.memories.record_recall(scope, [memory.memory_id])

    after = await container.memories.get(scope, memory.memory_id)
    assert after.updated_at == before
    assert after.last_recalled_at is not None


async def test_recall_is_scoped(container, scope):
    """Another tenant's search must not touch this tenant's counters."""
    from app.models.scope import MemoryScope

    memory = await _store(container, scope, "User is allergic to peanuts.")
    other = MemoryScope(user_id="somebody-else")

    assert await container.memories.record_recall(other, [memory.memory_id]) == 0
    assert (await container.memories.get(scope, memory.memory_id)).recall_count == 0


async def test_a_broken_counter_never_fails_a_search(container, scope, monkeypatch):
    """Losing a recall count costs a little precision in a decision months away.
    Failing the read costs the user their answer now."""

    async def boom(*_args, **_kwargs):
        raise RuntimeError("the database blinked")

    monkeypatch.setattr(container.memories, "record_recall", boom)

    await _store(container, scope, "User is allergic to peanuts.")
    from app.models.requests import SearchMemoryRequest

    results = await container.memory_service.search(
        scope, SearchMemoryRequest(user_id=scope.user_id, query="peanuts allergy")
    )
    assert results, "the search must still answer"


async def test_tracking_can_be_switched_off(container, scope):
    container.settings.track_recall = False
    memory = await _store(container, scope, "User is allergic to peanuts.")

    from app.models.requests import SearchMemoryRequest

    await container.memory_service.search(
        scope, SearchMemoryRequest(user_id=scope.user_id, query="peanuts allergy")
    )
    assert (await container.memories.get(scope, memory.memory_id)).recall_count == 0


async def test_nothing_reads_the_signal_yet(consolidator, container, scope):
    """Phase 0 collects; Phase 1 does not consume. A memory nobody has ever
    recalled is still perfectly valid, and expiring it now — before there is
    enough history to set a threshold against — would be guessing."""
    memory = await _store(container, scope, "User prefers window seats.")

    result = await consolidator.run(scope)

    assert memory.memory_id not in result.lapsed
    assert (await container.memories.get(scope, memory.memory_id)).status is MemoryStatus.ACTIVE


# --------------------------------------------------------------------------- #
# Phase 1 — a closed validity window
# --------------------------------------------------------------------------- #


async def test_a_lapsed_memory_is_expired(consolidator, container, scope):
    """Not stale, not unpopular — false. Dream sets `validUntil` when it
    supersedes something, and extraction sets it for anything the user bounded
    themselves."""
    lapsed = await _store(
        container, scope, "User is on leave this week.",
        valid_until=utcnow() - timedelta(days=1),
    )

    result = await consolidator.run(scope)

    assert lapsed.memory_id in result.lapsed
    assert (await container.memories.get(scope, lapsed.memory_id)).status is MemoryStatus.EXPIRED


async def test_expiring_is_not_deleting(consolidator, container, scope):
    """The whole reason this is safe to automate: being wrong costs a status
    flip, not a row."""
    lapsed = await _store(
        container, scope, "User is on leave this week.",
        valid_until=utcnow() - timedelta(days=1),
    )

    await consolidator.run(scope)

    stored = await container.memories.get(scope, lapsed.memory_id)
    assert stored is not None
    assert stored.content == "User is on leave this week."


async def test_a_still_valid_window_is_left_alone(consolidator, container, scope):
    future = await _store(
        container, scope, "User is travelling until next month.",
        valid_until=utcnow() + timedelta(days=30),
    )

    assert future.memory_id not in (await consolidator.run(scope)).lapsed


async def test_an_open_ended_memory_is_left_alone(consolidator, container, scope):
    """No `validUntil` means "still true", not "unknown, assume dead"."""
    memory = await _store(container, scope, "User is allergic to peanuts.")

    assert memory.memory_id not in (await consolidator.run(scope)).lapsed


# --------------------------------------------------------------------------- #
# The guard rails — the half that matters
# --------------------------------------------------------------------------- #


async def test_pattern_evidence_is_never_expired(consolidator, container, scope):
    """Expire the evidence and the pattern is left asserting something with
    nothing behind it — which validation already refuses to create, so it must
    not be reachable by the back door either."""
    evidence = await _store(
        container, scope, "User ran on Monday.",
        valid_until=utcnow() - timedelta(days=1),
    )
    await _store(
        container, scope, "User exercises most mornings.",
        memory_type=MemoryType.PATTERN.value,
        source_memory_ids=[evidence.memory_id],
        explicit=False,
    )

    result = await consolidator.run(scope)

    assert evidence.memory_id in result.protected
    assert evidence.memory_id not in result.lapsed
    assert (
        await container.memories.get(scope, evidence.memory_id)
    ).status is MemoryStatus.ACTIVE


async def test_patterns_themselves_are_never_expired(consolidator, container, scope):
    """A pattern is synthesis output, not an observation with a shelf life.
    Retiring one belongs to synthesis, which knows whether its evidence holds."""
    pattern = await _store(
        container, scope, "User exercises most mornings.",
        memory_type=MemoryType.PATTERN.value,
        source_memory_ids=["mem_whatever"],
        valid_until=utcnow() - timedelta(days=1),
        explicit=False,
    )

    assert pattern.memory_id not in (await consolidator.run(scope)).lapsed


async def test_a_dry_run_changes_nothing(consolidator, container, scope):
    """How you check a threshold before trusting it with real data."""
    lapsed = await _store(
        container, scope, "User is on leave this week.",
        valid_until=utcnow() - timedelta(days=1),
    )

    result = await consolidator.run(scope, dry_run=True)

    assert lapsed.memory_id in result.lapsed, "it still reports what it would do"
    assert (await container.memories.get(scope, lapsed.memory_id)).status is MemoryStatus.ACTIVE


async def test_consolidation_is_scoped(consolidator, container, scope):
    from app.models.scope import MemoryScope

    lapsed = await _store(
        container, scope, "User is on leave this week.",
        valid_until=utcnow() - timedelta(days=1),
    )

    result = await consolidator.run(MemoryScope(user_id="somebody-else"))

    assert lapsed.memory_id not in result.lapsed
    assert result.scanned == 0


async def test_it_needs_no_model_and_no_embedding(consolidator, container, scope, llm):
    """A nightly pass over a thousand users must cost a thousand indexed
    queries, not a thousand model calls. That property is the reason more rules
    can be added here later without the cost becoming a decision."""
    await _store(
        container, scope, "User is on leave this week.",
        valid_until=utcnow() - timedelta(days=1),
    )
    calls_before = len(llm.calls)
    embeds_before = container.embeddings.call_count

    await consolidator.run(scope)

    assert len(llm.calls) == calls_before
    assert container.embeddings.call_count == embeds_before


async def test_expired_memories_leave_the_search_index(consolidator, container, scope):
    """Going through the lifecycle rather than the repository is what unindexes
    them — the same path "forget that" takes."""
    from app.models.requests import SearchMemoryRequest

    await _store(
        container, scope, "User is on leave this week.",
        valid_until=utcnow() - timedelta(days=1),
    )
    await consolidator.run(scope)

    results = await container.memory_service.search(
        scope, SearchMemoryRequest(user_id=scope.user_id, query="on leave", latest_only=True)
    )
    assert not [r for r in results if "on leave" in r.memory.content]


async def test_a_synthesis_run_consolidates_first(container, scope):
    """Clustering over facts that lapsed in March finds patterns in things that
    are no longer true."""
    lapsed = await _store(
        container, scope, "User is on leave this week.",
        valid_until=utcnow() - timedelta(days=1),
    )

    await container.dream_service.request_synthesis(
        scope, wait=True, force=True, min_memories=2, requested_by="test"
    )

    assert (await container.memories.get(scope, lapsed.memory_id)).status is MemoryStatus.EXPIRED
