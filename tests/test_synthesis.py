"""Synthesis engine tests.

The load-bearing property here is idempotency. Synthesis runs on a schedule, so
a duplicate-detection bug does not produce one wrong pattern — it produces a new
one every night until retrieval is drowned in near-identical generalisations.
"""

from __future__ import annotations

import pytest

from app.dream.clustering import cluster_memories
from app.models.enums import JobStatus, MemoryStatus, MemoryType

pytestmark = pytest.mark.asyncio

FITNESS_MEMORIES = [
    "User runs 40 km per week.",
    "User goes to the gym three times a week.",
    "User tracks workouts with a Garmin watch.",
    "User is training for a marathon.",
    "User runs long distances every weekend.",
]


def synthesis_response(content: str, source_ids: list[str], confidence=0.9, importance=0.8) -> dict:
    return {
        "action": "SYNTHESIZE",
        "reason": "a recurring theme across several memories",
        "pattern": {
            "content": content,
            "memoryType": "pattern",
            "confidence": confidence,
            "importance": importance,
            "sourceMemoryIds": source_ids,
        },
    }


NOOP_RESPONSE = {"action": "NOOP", "reason": "no pattern here", "pattern": None}


@pytest.fixture
async def fitness_memories(store_memory):
    return [
        await store_memory(content, memory_type="behavior", importance=0.7)
        for content in FITNESS_MEMORIES
    ]


# --------------------------------------------------------------------------- #
# Creating patterns
# --------------------------------------------------------------------------- #


async def test_multiple_memories_become_a_pattern(container, scope, fitness_memories):
    job = await container.dream_service.request_synthesis(scope, wait=True)

    assert job.status == JobStatus.COMPLETED
    assert job.patterns_created >= 1
    assert job.memories_analyzed == len(fitness_memories)

    patterns = await container.memories.find_patterns(scope)
    assert patterns
    assert patterns[0].memory_type == MemoryType.PATTERN.value


async def test_pattern_sources_are_traceable_and_real(container, scope, fitness_memories):
    await container.dream_service.request_synthesis(scope, wait=True)
    pattern = (await container.memories.find_patterns(scope))[0]

    source_ids = {memory.memory_id for memory in fitness_memories}
    assert pattern.source_memory_ids
    assert set(pattern.source_memory_ids).issubset(source_ids)

    # And the edges exist, not just the inline list.
    detail = await container.memory_service.get_detail(scope, pattern.memory_id)
    assert {row.memory_id for row in detail.source_memories} <= source_ids


async def test_synthesis_never_modifies_its_sources(container, scope, fitness_memories):
    before = {m.memory_id: (m.content, m.status, m.updated_at) for m in fitness_memories}

    await container.dream_service.request_synthesis(scope, wait=True)

    for memory_id, (content, status, _) in before.items():
        after = await container.memories.get(scope, memory_id)
        assert after.content == content
        assert after.status == status


async def test_pattern_is_marked_inferred_not_explicit(container, scope, fitness_memories):
    await container.dream_service.request_synthesis(scope, wait=True)
    pattern = (await container.memories.find_patterns(scope))[0]

    # A generalisation is inference. Callers must be able to tell it apart from
    # something the user actually said.
    assert pattern.explicit is False


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


async def test_repeated_runs_do_not_duplicate_patterns(container, scope, fitness_memories):
    await container.dream_service.request_synthesis(scope, wait=True)
    baseline = len(await container.memories.find_patterns(scope))
    assert baseline >= 1

    for _ in range(4):
        await container.dream_service.request_synthesis(scope, wait=True, force=True)

    assert len(await container.memories.find_patterns(scope)) == baseline


async def test_source_overlap_blocks_a_duplicate_before_the_model_is_called(
    container, scope, fitness_memories, llm
):
    """The cheap check runs first: if a pattern already rests on this evidence,
    there is no question worth asking."""
    await container.dream_service.request_synthesis(scope, wait=True)
    calls_before = len(llm.calls)

    job = await container.dream_service.request_synthesis(scope, wait=True)

    assert job.patterns_skipped >= 1
    synthesis_calls = [
        call for call in llm.calls[calls_before:] if call["payload"].get("task") == "synthesize"
    ]
    assert synthesis_calls == []


async def test_semantically_identical_pattern_from_new_evidence_updates_in_place(
    container, scope, store_memory, llm
):
    """Same idea, different evidence: the second check catches what source
    overlap cannot see."""
    # At least `min_memories` (4 in the test settings), or the run is skipped.
    first_batch = [
        await store_memory(content, memory_type="behavior")
        for content in FITNESS_MEMORIES[:4]
    ]
    llm.push(
        synthesis_response(
            "User follows a structured fitness routine focused on marathon training.",
            [m.memory_id for m in first_batch],
        )
    )
    await container.dream_service.request_synthesis(scope, wait=True)
    patterns = await container.memories.find_patterns(scope)
    assert len(patterns) == 1
    original_id = patterns[0].memory_id

    # New supporting memories, and a proposal that says the same thing.
    second_batch = [
        await store_memory(content, memory_type="behavior")
        for content in (
            "User does interval training on Tuesdays.",
            "User follows a weekly running plan.",
            "User logs every run in a training app.",
        )
    ]
    llm.push_many(
        [
            synthesis_response(
                "User follows a structured fitness routine focused on marathon training.",
                [m.memory_id for m in second_batch],
            )
        ]
        * 5
    )
    await container.dream_service.request_synthesis(scope, wait=True, force=True)

    patterns = await container.memories.find_patterns(scope)
    assert len(patterns) == 1, "an equivalent pattern must be updated, not duplicated"
    assert patterns[0].memory_id == original_id, "the pattern keeps its id"


async def test_reinforcement_raises_confidence_but_never_to_certainty(
    container, scope, store_memory, llm
):
    batch = [await store_memory(c, memory_type="behavior") for c in FITNESS_MEMORIES[:4]]
    llm.push(synthesis_response("User is a dedicated endurance athlete.", [m.memory_id for m in batch], confidence=0.7))
    await container.dream_service.request_synthesis(scope, wait=True)

    extra = [
        await store_memory(c, memory_type="behavior")
        for c in ("User cycles on rest days.", "User swims weekly.", "User stretches daily.")
    ]
    llm.push_many(
        [synthesis_response("User is a dedicated endurance athlete.", [m.memory_id for m in extra], confidence=1.0)] * 5
    )
    await container.dream_service.request_synthesis(scope, wait=True, force=True)

    pattern = (await container.memories.find_patterns(scope))[0]
    assert pattern.confidence > 0.7
    # An inferred pattern must never claim the certainty of direct testimony.
    assert pattern.confidence <= 0.97


# --------------------------------------------------------------------------- #
# Cases that should NOT synthesise
# --------------------------------------------------------------------------- #


async def test_too_few_memories_skips_the_run(container, scope, store_memory):
    await store_memory("User likes coffee.", memory_type="preference")

    job = await container.dream_service.request_synthesis(scope, wait=True)

    assert job.status == JobStatus.SKIPPED
    assert "required" in (job.error or "")
    assert await container.memories.find_patterns(scope) == []


async def test_weak_evidence_produces_no_pattern(container, scope, fitness_memories, llm):
    llm.push_many([NOOP_RESPONSE] * 10)

    job = await container.dream_service.request_synthesis(scope, wait=True)

    assert job.patterns_created == 0
    assert await container.memories.find_patterns(scope) == []


async def test_pattern_below_the_quality_bar_is_discarded(container, scope, fitness_memories, llm):
    llm.push_many(
        [synthesis_response("A weak guess.", [m.memory_id for m in fitness_memories], confidence=0.2, importance=0.1)]
        * 10
    )

    job = await container.dream_service.request_synthesis(scope, wait=True)

    assert job.patterns_created == 0
    assert await container.memories.find_patterns(scope) == []


async def test_invented_source_ids_are_dropped_and_can_sink_the_pattern(
    container, scope, fitness_memories, llm
):
    """Sources are validated against the cluster the model was shown.

    A pattern whose evidence cannot be traced is worse than no pattern, so once
    the fabricated ids are removed there must still be enough real ones.
    """
    llm.push_many(
        [synthesis_response("Fabricated pattern.", ["mem_fake_1", "mem_fake_2", "mem_fake_3"])] * 10
    )

    job = await container.dream_service.request_synthesis(scope, wait=True)

    assert job.patterns_created == 0
    assert await container.memories.find_patterns(scope) == []


async def test_patterns_are_excluded_from_the_evidence_pool(container, scope, fitness_memories):
    """Patterns must not feed on patterns, or generalisations compound."""
    await container.dream_service.request_synthesis(scope, wait=True)
    assert await container.memories.find_patterns(scope)

    candidates = await container.synthesis._selector.select(scope)
    types = {memory.memory_type for memory in candidates.memories}
    assert MemoryType.PATTERN.value not in types


async def test_synthesis_disabled_skips_the_job(container, scope, fitness_memories):
    container.synthesis._config.enabled = False
    try:
        job = await container.dream_service.request_synthesis(scope, wait=True)
        assert job.status == JobStatus.SKIPPED
        assert "disabled" in (job.error or "")
    finally:
        container.synthesis._config.enabled = True


# --------------------------------------------------------------------------- #
# Pattern maintenance
# --------------------------------------------------------------------------- #


async def test_changing_a_source_flags_the_pattern_stale_without_deleting_it(
    container, scope, fitness_memories, candidate, llm
):
    await container.dream_service.request_synthesis(scope, wait=True)
    pattern = (await container.memories.find_patterns(scope))[0]
    assert pattern.stale is False

    source_id = pattern.source_memory_ids[0]
    llm.push(
        {
            "action": "SUPERSEDE",
            "reason": "training plan changed",
            "memory": {
                "content": "User now runs 60 km per week.",
                "memoryType": "behavior",
                "confidence": 0.95,
                "importance": 0.7,
                "temporalNature": "current",
            },
            "targetMemoryIds": [source_id],
        }
    )
    await container.dream_engine.process(
        scope, candidate("User now runs 60 km per week.", memory_type="behavior")
    )

    refreshed = await container.memories.get(scope, pattern.memory_id)
    assert refreshed.stale is True
    # Flagged for re-evaluation, never auto-deleted.
    assert refreshed.status == MemoryStatus.ACTIVE


# --------------------------------------------------------------------------- #
# Clustering
# --------------------------------------------------------------------------- #


async def test_clustering_groups_related_memories(make_memory, embeddings):
    memories = []
    for content in [*FITNESS_MEMORIES[:3], "User's favourite colour is blue.", "User owns a red car."]:
        memory = make_memory(content)
        memory.embedding = await embeddings.embed(content)
        memories.append(memory)

    clusters = cluster_memories(
        memories, similarity_threshold=0.10, min_cluster_size=2, max_cluster_size=10
    )

    assert clusters
    largest = clusters[0]
    # The running memories belong together; the colour and the car do not.
    assert largest.size >= 2
    assert largest.cohesion > 0


async def test_clustering_needs_a_minimum_size(make_memory, embeddings):
    memory = make_memory("User runs 40 km per week.")
    memory.embedding = await embeddings.embed(memory.content)

    assert cluster_memories([memory], min_cluster_size=3) == []


async def test_clustering_ignores_memories_without_embeddings(make_memory):
    memories = [make_memory(f"Memory {i}") for i in range(5)]
    assert cluster_memories(memories, min_cluster_size=2) == []
