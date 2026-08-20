"""Supersede engine tests.

Most of these script the LLM response explicitly rather than relying on the fake
reasoner's heuristics. The heuristics are tested elsewhere; here the subject is
the *engine* — does it validate the proposal, refuse what it should refuse, and
transition state correctly regardless of what the model said?
"""

from __future__ import annotations

import pytest

from app.models.enums import DreamAction, MemoryStatus, MemoryType, TemporalNature

pytestmark = pytest.mark.asyncio


def supersede_response(content: str, target_ids: list[str], memory_type: str = "profile") -> dict:
    return {
        "action": "SUPERSEDE",
        "reason": "the new statement contradicts the stored fact",
        "memory": {
            "content": content,
            "memoryType": memory_type,
            "confidence": 0.95,
            "importance": 0.8,
            "temporalNature": "current",
        },
        "targetMemoryIds": target_ids,
    }


# --------------------------------------------------------------------------- #
# Cases that SHOULD supersede
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("existing", "incoming", "memory_type"),
    [
        ("User lives in Chennai.", "User lives in Bangalore.", "profile"),
        ("User uses MongoDB in production.", "User uses PostgreSQL in production.", "fact"),
        ("User works at Company A.", "User works at Company B.", "profile"),
        ("User prefers dark mode.", "User prefers light mode.", "preference"),
    ],
)
async def test_contradiction_supersedes(
    container, scope, store_memory, candidate, llm, existing, incoming, memory_type
):
    old = await store_memory(existing, memory_type=memory_type)
    llm.push(supersede_response(incoming, [old.memory_id], memory_type))

    result = await container.dream_engine.process(
        scope, candidate(incoming, memory_type=memory_type)
    )

    assert result.action == DreamAction.SUPERSEDE
    assert result.superseded_memory_ids == [old.memory_id]

    refreshed = await container.memories.get(scope, old.memory_id)
    assert refreshed.status == MemoryStatus.SUPERSEDED
    assert refreshed.superseded_by == result.memory_id
    # Non-destructive: the original text survives untouched.
    assert refreshed.content == existing


async def test_superseded_memory_keeps_its_content_and_gains_a_pointer(
    container, scope, store_memory, candidate, llm
):
    old = await store_memory("User lives in Chennai.", memory_type="profile")
    llm.push(supersede_response("User lives in Bangalore.", [old.memory_id]))

    result = await container.dream_engine.process(
        scope, candidate("User lives in Bangalore.", memory_type="profile")
    )

    refreshed = await container.memories.get(scope, old.memory_id)
    assert refreshed.content == "User lives in Chennai."
    assert refreshed.superseded_by == result.memory_id
    assert refreshed.valid_until is not None

    new = await container.memories.get(scope, result.memory_id)
    assert new.valid_until is None
    assert old.memory_id in new.source_memory_ids


async def test_supersede_can_retire_several_memories_at_once(
    container, scope, store_memory, candidate, llm
):
    first = await store_memory("User lives in Chennai.", memory_type="profile")
    second = await store_memory("User is based in Tamil Nadu.", memory_type="profile")
    llm.push(
        supersede_response("User lives in Bangalore.", [first.memory_id, second.memory_id])
    )

    result = await container.dream_engine.process(
        scope, candidate("User lives in Bangalore.", memory_type="profile")
    )

    assert set(result.superseded_memory_ids) == {first.memory_id, second.memory_id}


# --------------------------------------------------------------------------- #
# Cases that should NOT supersede
# --------------------------------------------------------------------------- #


async def test_additive_detail_is_not_a_contradiction(container, scope, store_memory, candidate):
    """The fake reasoner should treat enrichment as MERGE, never SUPERSEDE."""
    await store_memory("User owns a dog named Rex.", memory_type="relationship")

    result = await container.dream_engine.process(
        scope,
        candidate("User owns a three-year-old Golden Retriever named Rex.", memory_type="relationship"),
    )

    assert result.action != DreamAction.SUPERSEDE


async def test_unrelated_memory_is_left_alone(container, scope, store_memory, candidate):
    existing = await store_memory("User owns a dog named Rex.", memory_type="relationship")

    result = await container.dream_engine.process(
        scope, candidate("User is learning to play the piano.", memory_type="skill")
    )

    assert result.action == DreamAction.ADD
    refreshed = await container.memories.get(scope, existing.memory_id)
    assert refreshed.status == MemoryStatus.ACTIVE


async def test_permanent_facts_are_protected_even_when_the_model_says_otherwise(
    container, scope, store_memory, candidate, llm
):
    """The backend refuses, whatever the model proposes.

    Where someone was born does not stop being true when they move. This is
    enforced in code rather than trusted to the prompt, because one bad decision
    would otherwise destroy an unrecoverable fact.
    """
    birthplace = await store_memory(
        "User was born in Chennai.",
        memory_type="profile",
        temporal_nature=TemporalNature.PERMANENT,
    )
    llm.push(supersede_response("User lives in Bangalore.", [birthplace.memory_id]))

    result = await container.dream_engine.process(
        scope, candidate("User lives in Bangalore.", memory_type="profile")
    )

    assert result.action == DreamAction.ADD
    assert any("permanent" in note for note in result.notes)

    refreshed = await container.memories.get(scope, birthplace.memory_id)
    assert refreshed.status == MemoryStatus.ACTIVE


async def test_patterns_are_never_offered_as_supersede_candidates(
    container, scope, store_memory, candidate, embeddings
):
    """A synthesised pattern must not be retired by an incoming raw fact.

    The protection is at candidate selection: patterns are excluded from
    `find_related` entirely, so the reasoner is never even shown one and cannot
    propose retiring it.
    """
    pattern = await store_memory(
        "User consistently prefers early-morning work.",
        memory_type=MemoryType.PATTERN.value,
    )

    related = await container.retriever.find_related(
        scope,
        embedding=await embeddings.embed("User prefers early morning work."),
        memory_type=MemoryType.PATTERN.value,
        similarity_threshold=0.0,
    )
    assert pattern.memory_id not in {memory.memory_id for memory, _ in related}

    result = await container.dream_engine.process(
        scope, candidate("User slept in today.", memory_type="event")
    )
    assert result.action == DreamAction.ADD

    refreshed = await container.memories.get(scope, pattern.memory_id)
    assert refreshed.status == MemoryStatus.ACTIVE


async def test_supersede_guard_refuses_patterns_and_permanent_facts(make_memory):
    """Defence in depth for the guard itself.

    Candidate selection already excludes patterns, so this path should be
    unreachable in normal operation — which is exactly why it is worth testing
    directly rather than through the pipeline.
    """
    from app.dream.engine import DreamEngine

    pattern = make_memory("A pattern.", memory_type=MemoryType.PATTERN.value)
    permanent = make_memory(
        "User was born in Chennai.",
        memory_type="profile",
        temporal_nature=TemporalNature.PERMANENT,
    )
    ordinary = make_memory("User lives in Chennai.", memory_type="profile")

    kept, notes = DreamEngine._protect_from_supersede([pattern, permanent, ordinary])

    assert kept == [ordinary]
    assert any("pattern" in note for note in notes)
    assert any("permanent" in note for note in notes)


async def test_supersede_disabled_falls_back_to_add(
    settings, database, llm, embeddings, store_memory, candidate, scope
):
    from app.container import DreamContainer
    from app.providers.queue.inline_queue import InlineJobQueue
    from app.providers.vector_store.memory_store import InMemoryVectorStore

    settings.dream.supersede.enabled = False
    container = await DreamContainer.create(
        settings,
        database=database,
        llm=llm,
        embeddings=embeddings,
        vectors=InMemoryVectorStore(),
        queue=InlineJobQueue(),
        ensure_schema=False,
    )

    memory = await container.memories.insert(
        __import__("app.models.memory", fromlist=["Memory"]).Memory(
            client_id=scope.client_id,
            bot_id=scope.bot_id,
            user_id=scope.user_id,
            content="User lives in Chennai.",
            memory_type="profile",
            embedding=await embeddings.embed("User lives in Chennai."),
        )
    )
    await container.retriever.index_memory(scope, memory)
    llm.push(supersede_response("User lives in Bangalore.", [memory.memory_id]))

    result = await container.dream_engine.process(
        scope, candidate("User lives in Bangalore.", memory_type="profile")
    )

    assert result.action == DreamAction.ADD
    assert any("disabled" in note for note in result.notes)


# --------------------------------------------------------------------------- #
# Validation of model output
# --------------------------------------------------------------------------- #


async def test_invented_target_ids_are_dropped(
    container, scope, store_memory, candidate, llm
):
    """An id the model was never shown must never be acted on.

    This is what stops a model reaching another tenant's memory by guessing.
    """
    real = await store_memory("User lives in Chennai.", memory_type="profile")
    llm.push(
        supersede_response(
            "User lives in Bangalore.", [real.memory_id, "mem_totally_made_up"]
        )
    )

    result = await container.dream_engine.process(
        scope, candidate("User lives in Bangalore.", memory_type="profile")
    )

    assert result.superseded_memory_ids == [real.memory_id]
    assert any("not supplied" in note for note in result.notes)


async def test_supersede_with_no_valid_targets_becomes_add(
    container, scope, store_memory, candidate, llm
):
    await store_memory("User lives in Chennai.", memory_type="profile")
    llm.push(supersede_response("User lives in Bangalore.", ["mem_does_not_exist"]))

    result = await container.dream_engine.process(
        scope, candidate("User lives in Bangalore.", memory_type="profile")
    )

    assert result.action == DreamAction.ADD
    assert result.memory is not None


async def test_replaying_a_supersede_is_idempotent(
    container, scope, store_memory, candidate, llm
):
    """A target already in a terminal state is skipped, not re-transitioned."""
    old = await store_memory("User lives in Chennai.", memory_type="profile")

    llm.push(supersede_response("User lives in Bangalore.", [old.memory_id]))
    first = await container.dream_engine.process(
        scope, candidate("User lives in Bangalore.", memory_type="profile")
    )
    assert first.superseded_memory_ids == [old.memory_id]

    # Same decision replayed against a memory that has already moved on.
    llm.push(supersede_response("User lives in Bangalore.", [old.memory_id]))
    second = await container.dream_engine.process(
        scope, candidate("User lives in Bangalore.", memory_type="profile")
    )

    refreshed = await container.memories.get(scope, old.memory_id)
    assert refreshed.status == MemoryStatus.SUPERSEDED
    assert refreshed.superseded_by == first.memory_id, "the pointer must not be rewritten"
    assert old.memory_id not in second.superseded_memory_ids


async def test_supersede_writes_an_audit_record(container, scope, store_memory, candidate, llm):
    old = await store_memory("User lives in Chennai.", memory_type="profile")
    llm.push(supersede_response("User lives in Bangalore.", [old.memory_id]))

    result = await container.dream_engine.process(
        scope, candidate("User lives in Bangalore.", memory_type="profile")
    )

    operation = await container.audit.get(result.operation_id)
    assert operation is not None
    assert operation.action == "SUPERSEDE"
    assert operation.memory_id == result.memory_id
    assert operation.target_memory_ids == [old.memory_id]
    assert operation.reason
    assert operation.model
    # The candidate set is recorded so a bad decision can be replayed exactly.
    assert old.memory_id in operation.candidate_memory_ids


# --------------------------------------------------------------------------- #
# The permanence guard must protect, not trap
# --------------------------------------------------------------------------- #


async def test_mutable_types_ignore_a_wrong_permanent_label(make_memory):
    """A preference is reversible by the person whose preference it is.

    Regression: "I like biryani" was extracted as `permanent`, so "I don't like
    biryani" could never supersede it. One mislabel at extraction time froze the
    memory beyond correction, turning a safety check into a trap.
    """
    from app.dream.engine import DreamEngine

    preference = make_memory(
        "User likes biryani.",
        memory_type="preference",
        temporal_nature=TemporalNature.PERMANENT,
    )

    kept, notes = DreamEngine._protect_from_supersede([preference])

    assert kept == [preference], "a preference must remain supersedable"
    assert any("mutable by nature" in note for note in notes)


@pytest.mark.parametrize(
    "memory_type", ["preference", "behavior", "goal", "project", "constraint", "skill"]
)
async def test_all_inherently_mutable_types_stay_supersedable(make_memory, memory_type):
    from app.dream.engine import DreamEngine

    memory = make_memory(
        "Something changeable.",
        memory_type=memory_type,
        temporal_nature=TemporalNature.PERMANENT,
    )
    kept, _ = DreamEngine._protect_from_supersede([memory])
    assert kept == [memory]


@pytest.mark.parametrize("memory_type", ["profile", "fact", "event"])
async def test_genuinely_permanent_facts_are_still_protected(make_memory, memory_type):
    """Birthplace does not stop being true because someone moved."""
    from app.dream.engine import DreamEngine

    memory = make_memory(
        "User was born in Chennai.",
        memory_type=memory_type,
        temporal_nature=TemporalNature.PERMANENT,
    )
    kept, notes = DreamEngine._protect_from_supersede([memory])

    assert kept == []
    assert any("permanent fact" in note for note in notes)


async def test_reversed_preference_supersedes_end_to_end(
    container, scope, store_memory, candidate, llm
):
    """The biryani case, through the engine."""
    original = await store_memory(
        "User likes biryani.",
        memory_type="preference",
        temporal_nature=TemporalNature.PERMANENT,
    )
    llm.push(supersede_response("User does not like biryani.", [original.memory_id], "preference"))

    result = await container.dream_engine.process(
        scope, candidate("User does not like biryani.", memory_type="preference")
    )

    assert result.action == DreamAction.SUPERSEDE
    assert original.memory_id in result.superseded_memory_ids

    refreshed = await container.memories.get(scope, original.memory_id)
    assert refreshed.status == MemoryStatus.SUPERSEDED
    assert refreshed.content == "User likes biryani."   # kept, not deleted
