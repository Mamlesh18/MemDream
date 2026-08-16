"""Merge engine tests: exact duplicates, semantic duplicates, enrichment,
partial overlap, and the cases that must be left alone."""

from __future__ import annotations

import pytest

from app.models.enums import DreamAction, MemoryStatus
from app.models.memory import content_fingerprint, normalize_content

pytestmark = pytest.mark.asyncio


def merge_response(content: str, target_ids: list[str], memory_type: str = "preference") -> dict:
    return {
        "action": "MERGE",
        "reason": "the new memory is a richer version of an existing one",
        "memory": {
            "content": content,
            "memoryType": memory_type,
            "confidence": 0.95,
            "importance": 0.6,
            "temporalNature": "current",
        },
        "targetMemoryIds": target_ids,
    }


# --------------------------------------------------------------------------- #
# Exact duplicates — the deterministic fast path
# --------------------------------------------------------------------------- #


async def test_exact_duplicate_is_noop_without_reaching_the_reasoner(
    container, scope, store_memory, candidate, llm
):
    await store_memory("User likes black coffee.", memory_type="preference")
    calls_before = len(llm.calls)

    result = await container.dream_engine.process(
        scope, candidate("User likes black coffee.", memory_type="preference")
    )

    assert result.action == DreamAction.NOOP
    assert len(llm.calls) == calls_before, "an exact duplicate must not cost a model call"


@pytest.mark.parametrize(
    "variant",
    [
        "User likes black coffee",  # no trailing period
        "user likes black coffee.",  # lower case
        "  User   likes  black   coffee.  ",  # whitespace
        "User likes black coffee!!",  # different punctuation
    ],
)
async def test_normalisation_catches_trivial_variations(
    container, scope, store_memory, candidate, variant
):
    await store_memory("User likes black coffee.", memory_type="preference")

    result = await container.dream_engine.process(
        scope, candidate(variant, memory_type="preference")
    )

    assert result.action == DreamAction.NOOP


async def test_content_fingerprint_is_stable_across_formatting():
    base = content_fingerprint("User likes black coffee.")
    assert content_fingerprint("  user LIKES black   coffee  ") == base
    assert content_fingerprint("User likes black coffee!") == base
    assert content_fingerprint("User likes green tea.") != base


async def test_normalize_content_folds_case_whitespace_and_punctuation():
    assert normalize_content("  User  Likes   Coffee.  ") == "user likes coffee"


# --------------------------------------------------------------------------- #
# Semantic duplicates and enrichment
# --------------------------------------------------------------------------- #


async def test_richer_memory_becomes_canonical(container, scope, store_memory, candidate, llm):
    """The specification's worked example, end to end."""
    original = await store_memory("User owns a dog named Rex.", memory_type="relationship")
    canonical_text = "User owns a three-year-old Golden Retriever named Rex."
    llm.push(merge_response(canonical_text, [original.memory_id], "relationship"))

    result = await container.dream_engine.process(
        scope,
        candidate("Rex is a three-year-old Golden Retriever.", memory_type="relationship"),
    )

    assert result.action == DreamAction.MERGE
    assert result.memory.content == canonical_text
    assert original.memory_id in result.merged_memory_ids

    merged = await container.memories.get(scope, original.memory_id)
    assert merged.status == MemoryStatus.MERGED
    assert merged.merged_into == result.memory_id
    # Non-destructive.
    assert merged.content == "User owns a dog named Rex."


async def test_canonical_memory_inherits_provenance(
    container, scope, store_memory, candidate, llm
):
    first = await store_memory("User owns a dog named Rex.", memory_type="relationship")
    second = await store_memory("Rex is a Golden Retriever.", memory_type="relationship")
    llm.push(
        merge_response(
            "User owns a three-year-old Golden Retriever named Rex.",
            [first.memory_id, second.memory_id],
            "relationship",
        )
    )

    result = await container.dream_engine.process(
        scope, candidate("Rex is three years old.", memory_type="relationship")
    )

    canonical = await container.memories.get(scope, result.memory_id)
    assert set(result.merged_memory_ids) == {first.memory_id, second.memory_id}
    assert set(canonical.source_memory_ids) >= {first.memory_id, second.memory_id}


async def test_merged_memories_leave_the_search_index(
    container, scope, store_memory, candidate, llm
):
    """Merged content lives on in the canonical memory; leaving the originals
    indexed would return the same fact twice under two ids."""
    original = await store_memory("User owns a dog named Rex.", memory_type="relationship")
    llm.push(
        merge_response(
            "User owns a three-year-old Golden Retriever named Rex.",
            [original.memory_id],
            "relationship",
        )
    )

    result = await container.dream_engine.process(
        scope, candidate("Rex is a three-year-old Golden Retriever.", memory_type="relationship")
    )

    hits = await container.vectors.search(
        scope, result.memory.embedding, top_k=10, allowed_ids=None
    )
    indexed = {hit.memory_id for hit in hits}
    assert original.memory_id not in indexed
    assert result.memory_id in indexed


async def test_merge_records_a_relationship(container, scope, store_memory, candidate, llm):
    original = await store_memory("User owns a dog named Rex.", memory_type="relationship")
    llm.push(merge_response("User owns a Golden Retriever named Rex.", [original.memory_id], "relationship"))

    result = await container.dream_engine.process(
        scope, candidate("Rex is a Golden Retriever.", memory_type="relationship")
    )

    detail = await container.memory_service.get_detail(scope, result.memory_id)
    assert original.memory_id in {row.memory_id for row in detail.relationships}


# --------------------------------------------------------------------------- #
# Cases that should NOT merge
# --------------------------------------------------------------------------- #


async def test_unrelated_memories_are_not_merged(container, scope, store_memory, candidate):
    existing = await store_memory("User owns a dog named Rex.", memory_type="relationship")

    result = await container.dream_engine.process(
        scope, candidate("User's production database is PostgreSQL.", memory_type="fact")
    )

    assert result.action == DreamAction.ADD
    refreshed = await container.memories.get(scope, existing.memory_id)
    assert refreshed.status == MemoryStatus.ACTIVE


async def test_different_subjects_are_not_merged(container, scope, store_memory, candidate):
    """Two pets are two memories, however similar the sentences."""
    await store_memory("User owns a dog named Rex.", memory_type="relationship")

    result = await container.dream_engine.process(
        scope, candidate("User owns a cat named Whiskers.", memory_type="relationship")
    )

    assert result.action != DreamAction.SUPERSEDE
    active = await container.memories.find(scope, statuses=[MemoryStatus.ACTIVE], limit=50)
    assert len(active) >= 2, "distinct subjects must remain distinct memories"


async def test_merge_targets_are_restricted_to_the_same_type(container, scope, make_memory):
    """A preference and a constraint about the same thing are two facts about
    it, not one fact stated twice."""
    preference = make_memory("User likes spicy food.", memory_type="preference")
    constraint = make_memory("User cannot eat spicy food.", memory_type="constraint")

    eligible = container.dream_engine.merge_engine.eligible_targets(
        "preference", [(preference, 0.9), (constraint, 0.9)]
    )

    assert [memory.memory_id for memory, _ in eligible] == [preference.memory_id]


async def test_merge_disabled_falls_back_to_add(container, scope, store_memory, candidate, llm):
    container.dream_engine.merge_engine._settings.enabled = False
    original = await store_memory("User owns a dog named Rex.", memory_type="relationship")
    llm.push(merge_response("User owns a Golden Retriever named Rex.", [original.memory_id], "relationship"))

    result = await container.dream_engine.process(
        scope, candidate("Rex is a Golden Retriever.", memory_type="relationship")
    )

    assert result.action == DreamAction.ADD
    assert any("disabled" in note for note in result.notes)
    container.dream_engine.merge_engine._settings.enabled = True


# --------------------------------------------------------------------------- #
# Information preservation
# --------------------------------------------------------------------------- #


async def test_a_lossy_canonical_text_is_flagged(container, scope, store_memory, candidate, llm):
    """The merge still applies — legitimate merges do compress — but the audit
    trail records that something may have been dropped."""
    original = await store_memory(
        "User owns a three-year-old Golden Retriever named Rex who was adopted in Chennai.",
        memory_type="relationship",
    )
    llm.push(merge_response("User owns a dog.", [original.memory_id], "relationship"))

    result = await container.dream_engine.process(
        scope, candidate("Rex is a Golden Retriever.", memory_type="relationship")
    )

    assert result.action == DreamAction.MERGE
    assert any("shorter" in note for note in result.notes)

    operation = await container.audit.get(result.operation_id)
    assert any("shorter" in note for note in operation.validation_notes)


async def test_information_loss_check_accepts_a_faithful_canonical(container, make_memory):
    engine = container.dream_engine.merge_engine
    original = make_memory("User owns a dog named Rex.")

    notes = engine.check_no_information_lost(
        "User owns a three-year-old Golden Retriever named Rex.",
        [original],
        "Rex is a three-year-old Golden Retriever.",
    )

    assert notes == []
