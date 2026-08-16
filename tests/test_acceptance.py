"""The seven acceptance scenarios from the specification.

These are the tests that decide whether the framework does what it claims. Each
one is written against the public service surface rather than internals, so they
keep their meaning if the implementation is rewritten underneath.
"""

from __future__ import annotations

import pytest

from app.models.enums import DreamAction, MemoryStatus, MemoryType
from app.models.requests import SearchMemoryRequest

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Scenario 1: Add
# --------------------------------------------------------------------------- #


async def test_scenario_1_new_fact_is_added(container, scope):
    outcome = await container.memory_service.add(scope, "I like black coffee.")

    assert outcome.results, "expected at least one memory to be extracted"
    result = outcome.primary
    assert result.action == DreamAction.ADD
    assert result.memory is not None

    stored = await container.memories.get(scope, result.memory_id)
    assert stored is not None
    assert stored.status == MemoryStatus.ACTIVE
    assert "coffee" in stored.content.lower()


# --------------------------------------------------------------------------- #
# Scenario 2: Duplicate -> MERGE or NOOP, never a second active memory
# --------------------------------------------------------------------------- #


async def test_scenario_2_duplicate_does_not_create_a_second_active_memory(
    container, scope
):
    await container.memory_service.add(scope, "I like black coffee.")
    outcome = await container.memory_service.add(scope, "I prefer black coffee.")

    result = outcome.primary
    assert result.action in (DreamAction.MERGE, DreamAction.NOOP)

    active = await container.memories.find(scope, statuses=[MemoryStatus.ACTIVE], limit=100)
    coffee = [m for m in active if "coffee" in m.content.lower()]
    assert len(coffee) == 1, f"expected one active coffee memory, got {[m.content for m in coffee]}"


async def test_scenario_2_exact_duplicate_is_noop_without_an_llm_call(
    container, scope, llm
):
    await container.memory_service.add(scope, "I like black coffee.")
    calls_before = len(llm.calls)

    outcome = await container.memory_service.add(scope, "I like black coffee.")

    assert outcome.primary.action == DreamAction.NOOP
    # Extraction still runs; the decision call is what the hash lookup saves.
    decision_calls = [
        call for call in llm.calls[calls_before:] if call["payload"].get("task") == "decide"
    ]
    assert decision_calls == [], "an exact duplicate should not reach the decision reasoner"


# --------------------------------------------------------------------------- #
# Scenario 3: Supersede
# --------------------------------------------------------------------------- #


async def test_scenario_3_new_location_supersedes_the_old_one(container, scope):
    first = await container.memory_service.add(scope, "I live in Chennai.")
    old_id = first.primary.memory_id

    second = await container.memory_service.add(scope, "I moved to Bangalore.")
    result = second.primary

    assert result.action == DreamAction.SUPERSEDE
    assert old_id in result.superseded_memory_ids

    old = await container.memories.get(scope, old_id)
    new = await container.memories.get(scope, result.memory_id)

    assert old.status == MemoryStatus.SUPERSEDED
    assert old.superseded_by == new.memory_id
    assert new.status == MemoryStatus.ACTIVE
    # Non-destructive: the old memory is still there in full.
    assert old.content


async def test_scenario_3_superseded_memory_gets_a_validity_boundary(container, scope):
    first = await container.memory_service.add(scope, "I live in Chennai.")
    old_id = first.primary.memory_id
    await container.memory_service.add(scope, "I moved to Bangalore.")

    old = await container.memories.get(scope, old_id)
    assert old.valid_until is not None, "a retired fact should have its window closed"


async def test_supersede_records_a_traceable_relationship(container, scope):
    first = await container.memory_service.add(scope, "I live in Chennai.")
    second = await container.memory_service.add(scope, "I moved to Bangalore.")

    detail = await container.memory_service.get_detail(scope, second.primary.memory_id)
    related_ids = {row.memory_id for row in detail.relationships}
    assert first.primary.memory_id in related_ids


# --------------------------------------------------------------------------- #
# Scenario 4: Synthesis
# --------------------------------------------------------------------------- #


async def test_scenario_4_synthesis_creates_a_traceable_pattern(container, scope, store_memory):
    sources = []
    for content in (
        "User prefers morning meetings.",
        "User frequently schedules meetings before 10 AM.",
        "User usually starts work early in the morning.",
        "User attends early morning calls.",
        "User blocks the morning for focused work.",
    ):
        sources.append(await store_memory(content, memory_type="behavior", importance=0.7))

    job = await container.dream_service.request_synthesis(scope, wait=True)

    assert job.patterns_created >= 1, f"no pattern created: {job.error}"

    patterns = await container.memories.find_patterns(scope)
    assert patterns
    pattern = patterns[0]

    assert pattern.memory_type == MemoryType.PATTERN.value
    assert pattern.status == MemoryStatus.ACTIVE
    # Traceability is mandatory.
    assert pattern.source_memory_ids
    assert set(pattern.source_memory_ids).issubset({m.memory_id for m in sources})
    # A pattern is inferred, not testimony.
    assert pattern.explicit is False

    # Source memories are untouched.
    for source in sources:
        refreshed = await container.memories.get(scope, source.memory_id)
        assert refreshed.status == MemoryStatus.ACTIVE
        assert refreshed.content == source.content


async def test_scenario_4_pattern_records_derived_from_edges(container, scope, store_memory):
    for content in (
        "User prefers morning meetings.",
        "User frequently schedules meetings before 10 AM.",
        "User usually starts work early in the morning.",
        "User attends early morning calls.",
    ):
        await store_memory(content, memory_type="behavior")

    await container.dream_service.request_synthesis(scope, wait=True)
    patterns = await container.memories.find_patterns(scope)
    assert patterns

    detail = await container.memory_service.get_detail(scope, patterns[0].memory_id)
    assert detail.source_memories, "a pattern must expose the memories it rests on"


# --------------------------------------------------------------------------- #
# Scenario 5: Idempotency
# --------------------------------------------------------------------------- #


async def test_scenario_5_synthesis_is_idempotent(container, scope, store_memory):
    for content in (
        "User prefers morning meetings.",
        "User frequently schedules meetings before 10 AM.",
        "User usually starts work early in the morning.",
        "User attends early morning calls.",
        "User blocks the morning for focused work.",
    ):
        await store_memory(content, memory_type="behavior")

    first = await container.dream_service.request_synthesis(scope, wait=True)
    after_first = len(await container.memories.find_patterns(scope))
    assert after_first >= 1

    # Same evidence, three more runs.
    for _ in range(3):
        await container.dream_service.request_synthesis(scope, wait=True, force=True)

    after_repeats = len(await container.memories.find_patterns(scope))
    assert after_repeats == after_first, (
        f"synthesis created duplicate patterns: {after_first} -> {after_repeats}"
    )
    assert first.patterns_created >= 1


# --------------------------------------------------------------------------- #
# Scenario 6: Tenant isolation
# --------------------------------------------------------------------------- #


async def test_scenario_6_clients_never_see_each_others_memories(
    container, scope, other_scope
):
    await container.memory_service.add(scope, "I live in Chennai and work at Acme.")
    await container.memory_service.add(other_scope, "I live in Chennai and work at Acme.")

    a_memories = await container.memories.find(scope, limit=100)
    b_memories = await container.memories.find(other_scope, limit=100)

    assert a_memories and b_memories
    assert {m.memory_id for m in a_memories}.isdisjoint({m.memory_id for m in b_memories})

    results = await container.memory_service.search(
        scope,
        SearchMemoryRequest(
            query="Where does the user live?",
            client_id=scope.client_id,
            bot_id=scope.bot_id,
            user_id=scope.user_id,
        ),
    )
    returned = {r.memory.memory_id for r in results}
    assert returned.isdisjoint({m.memory_id for m in b_memories})
    for result in results:
        assert result.memory.client_id == scope.client_id


async def test_scenario_6_identical_phone_yields_different_user_ids(scope, other_scope):
    # Same phone number, different client: the derived identities must not match,
    # or memory would flow between tenants.
    assert scope.user_id != other_scope.user_id


async def test_scenario_6_cross_scope_read_by_id_returns_nothing(
    container, scope, other_scope
):
    outcome = await container.memory_service.add(scope, "I live in Chennai.")
    memory_id = outcome.primary.memory_id

    # Possessing an id is not authorisation.
    assert await container.memories.get(other_scope, memory_id) is None


# --------------------------------------------------------------------------- #
# Scenario 7: Historical retrieval
# --------------------------------------------------------------------------- #


async def test_scenario_7_latest_only_hides_history(container, scope):
    await container.memory_service.add(scope, "I live in Chennai.")
    await container.memory_service.add(scope, "I moved to Bangalore.")

    def request(**kwargs):
        return SearchMemoryRequest(
            query="Where does the user live?",
            client_id=scope.client_id,
            bot_id=scope.bot_id,
            user_id=scope.user_id,
            **kwargs,
        )

    latest = await container.memory_service.search(scope, request(latest_only=True))
    assert latest
    assert all(r.memory.status == MemoryStatus.ACTIVE for r in latest)
    assert any("bangalore" in r.memory.content.lower() for r in latest)
    assert not any("chennai" in r.memory.content.lower() for r in latest)

    with_history = await container.memory_service.search(
        scope, request(include_superseded=True)
    )
    statuses = {r.memory.status for r in with_history}
    assert MemoryStatus.SUPERSEDED in statuses, "history should be retrievable by default"


async def test_scenario_7_active_memory_outranks_superseded_history(container, scope):
    await container.memory_service.add(scope, "I live in Chennai.")
    await container.memory_service.add(scope, "I moved to Bangalore.")

    results = await container.memory_service.search(
        scope,
        SearchMemoryRequest(
            query="Where does the user live?",
            client_id=scope.client_id,
            bot_id=scope.bot_id,
            user_id=scope.user_id,
            include_superseded=True,
        ),
    )
    assert results
    # The status weight exists precisely so current facts win ties.
    assert results[0].memory.status == MemoryStatus.ACTIVE
