"""Manual memory management: create, edit, forget, erase.

These bypass the Dream layer entirely. The properties worth protecting are that
they stay auditable, that they cannot rewrite history, and that the destructive
ones are hard to trigger by accident.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.exceptions import InvalidStateTransitionError
from app.main import create_app
from app.models.enums import MemoryStatus

pytestmark = pytest.mark.asyncio

PHONE = "+919999999999"
CLIENT = "client_test"
BOT = "bot_test"


@pytest.fixture
async def client(container):
    app = create_app()
    app.state.container = container
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


def scope_params(client_id: str = CLIENT) -> dict:
    return {"clientId": client_id, "botId": BOT, "phoneNumber": PHONE}


def body(**extra) -> dict:
    return {**scope_params(), **extra}


# --------------------------------------------------------------------------- #
# Manual creation
# --------------------------------------------------------------------------- #


async def test_manual_create_stores_content_verbatim(client, container, scope, llm):
    calls_before = len(llm.calls)

    res = await client.post(
        "/v1/memories/manual",
        json=body(content="User is allergic to peanuts.", memoryType="constraint", importance=0.95),
    )
    assert res.status_code == 201

    memory = res.json()["memory"]
    # Verbatim: no rewriting, no re-scoring.
    assert memory["content"] == "User is allergic to peanuts."
    assert memory["memoryType"] == "constraint"
    assert memory["importance"] == 0.95
    assert memory["status"] == "active"
    # No extraction and no decision call — that is the point of this endpoint.
    assert len(llm.calls) == calls_before


async def test_manual_create_bypasses_the_sensitivity_policy(client, container, scope):
    """The policy governs what is inferred from conversation, not what an
    operator explicitly asks to store."""
    res = await client.post(
        "/v1/memories/manual",
        json=body(content="User has a severe peanut allergy.", memoryType="constraint"),
    )
    assert res.status_code == 201

    stored = await container.memories.find(scope, statuses=[MemoryStatus.ACTIVE], limit=10)
    assert any("peanut" in m.content for m in stored)


async def test_manual_create_is_indexed_and_searchable(client, container, scope):
    await client.post(
        "/v1/memories/manual", json=body(content="User is allergic to peanuts.")
    )

    # Query shares vocabulary with the stored text: the offline embedder is
    # bag-of-words and has no stemming, so "allergies" would not match
    # "allergic". Retrieval quality is tested elsewhere; this is about indexing.
    found = await client.post(
        "/v1/memories/search", json=body(query="allergic peanuts", limit=5)
    )
    assert any("peanut" in m["content"] for m in found.json()["memories"])


async def test_manual_create_is_marked_as_manual(client, container, scope):
    res = await client.post("/v1/memories/manual", json=body(content="A manual fact."))
    memory = await container.memories.get(scope, res.json()["memory"]["memoryId"])
    assert memory.metadata.get("source") == "manual"


async def test_manual_create_writes_an_audit_row(client, container, scope):
    await client.post("/v1/memories/manual", json=body(content="A manual fact."))

    operations = await container.audit.list_for_scope(scope, limit=10)
    manual = [op for op in operations if op.action == "MANUAL_ADD"]
    assert manual
    # No model was involved, and the audit trail should say so.
    assert manual[0].provider == "manual"
    assert manual[0].model is None


async def test_manual_create_returns_the_existing_row_instead_of_duplicating(client):
    first = await client.post("/v1/memories/manual", json=body(content="User likes tea."))
    second = await client.post("/v1/memories/manual", json=body(content="User likes tea."))

    assert second.json()["memory"]["memoryId"] == first.json()["memory"]["memoryId"]


async def test_allow_duplicate_forces_a_second_row(client):
    first = await client.post("/v1/memories/manual", json=body(content="User likes tea."))
    second = await client.post(
        "/v1/memories/manual", json=body(content="User likes tea.", allowDuplicate=True)
    )

    assert second.json()["memory"]["memoryId"] != first.json()["memory"]["memoryId"]


# --------------------------------------------------------------------------- #
# Manual editing
# --------------------------------------------------------------------------- #


async def test_edit_changes_only_the_supplied_fields(client, container, scope):
    created = await client.post(
        "/v1/memories/manual",
        json=body(content="User lives in Chennai.", memoryType="profile", importance=0.5),
    )
    memory_id = created.json()["memory"]["memoryId"]

    res = await client.patch(f"/v1/memories/{memory_id}", json=body(importance=0.9))
    assert res.status_code == 200

    memory = res.json()["memory"]
    assert memory["importance"] == 0.9
    assert memory["content"] == "User lives in Chennai."   # untouched
    assert memory["memoryType"] == "profile"               # untouched


async def test_edit_keeps_the_same_id(client):
    created = await client.post("/v1/memories/manual", json=body(content="Original text."))
    memory_id = created.json()["memory"]["memoryId"]

    res = await client.patch(f"/v1/memories/{memory_id}", json=body(content="Corrected text."))

    assert res.json()["memory"]["memoryId"] == memory_id
    assert res.json()["memory"]["content"] == "Corrected text."


async def test_editing_content_reindexes_it(client, container, scope):
    """An index entry that disagrees with its own text is a silent retrieval bug."""
    created = await client.post(
        "/v1/memories/manual", json=body(content="User plays the violin.")
    )
    memory_id = created.json()["memory"]["memoryId"]

    await client.patch(f"/v1/memories/{memory_id}", json=body(content="User plays the trumpet."))

    found = await client.post("/v1/memories/search", json=body(query="trumpet", limit=5))
    assert any("trumpet" in m["content"] for m in found.json()["memories"])


async def test_edit_writes_an_audit_row_naming_the_fields(client, container, scope):
    created = await client.post("/v1/memories/manual", json=body(content="Original."))
    memory_id = created.json()["memory"]["memoryId"]

    await client.patch(f"/v1/memories/{memory_id}", json=body(content="Corrected.", importance=0.8))

    operations = await container.audit.list_for_scope(scope, limit=10)
    edits = [op for op in operations if op.action == "MANUAL_UPDATE"]
    assert edits
    assert "content" in edits[0].reason
    # The previous text is retained so the edit can be reviewed.
    assert edits[0].input_content == "Original."


async def test_history_cannot_be_edited(client, container, scope):
    """Superseded and merged rows are history. History is not rewritten."""
    await container.memory_service.add(scope, "I live in Chennai.")
    outcome = await container.memory_service.add(scope, "I moved to Bangalore.")
    retired_id = outcome.primary.superseded_memory_ids[0]

    res = await client.patch(f"/v1/memories/{retired_id}", json=body(content="Rewritten history."))
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "invalid_state_transition"

    unchanged = await container.memories.get(scope, retired_id)
    assert "Chennai" in unchanged.content
    assert "Rewritten" not in unchanged.content


async def test_edit_requires_at_least_one_field(client):
    created = await client.post("/v1/memories/manual", json=body(content="Something."))
    res = await client.patch(f"/v1/memories/{created.json()['memory']['memoryId']}", json=body())
    assert res.status_code == 422


async def test_edit_of_a_missing_memory_is_404(client):
    res = await client.patch("/v1/memories/mem_nope", json=body(content="x"))
    assert res.status_code == 404


# --------------------------------------------------------------------------- #
# Forgetting one memory
# --------------------------------------------------------------------------- #


async def test_forget_retires_without_deleting(client, container, scope):
    created = await client.post("/v1/memories/manual", json=body(content="A regrettable fact."))
    memory_id = created.json()["memory"]["memoryId"]

    res = await client.delete(f"/v1/memories/{memory_id}", params=scope_params())
    assert res.status_code == 200
    assert res.json()["result"] == "expired"
    assert res.json()["permanent"] is False

    # Still readable — retired, not destroyed.
    memory = await container.memories.get(scope, memory_id)
    assert memory is not None
    assert memory.status == MemoryStatus.EXPIRED
    assert memory.content == "A regrettable fact."


async def test_forgotten_memories_leave_the_search_index(client, container, scope):
    created = await client.post(
        "/v1/memories/manual", json=body(content="User plays the harmonica.")
    )
    await client.delete(
        f"/v1/memories/{created.json()['memory']['memoryId']}", params=scope_params()
    )

    found = await client.post("/v1/memories/search", json=body(query="harmonica", limit=5))
    assert not any("harmonica" in m["content"] for m in found.json()["memories"])


async def test_permanent_delete_erases_the_row(client, container, scope):
    created = await client.post("/v1/memories/manual", json=body(content="A leaked secret."))
    memory_id = created.json()["memory"]["memoryId"]

    res = await client.delete(
        f"/v1/memories/{memory_id}", params={**scope_params(), "permanent": "true"}
    )
    assert res.status_code == 200
    assert res.json()["result"] == "deleted"
    assert res.json()["permanent"] is True

    assert await container.memories.get(scope, memory_id) is None


async def test_permanent_delete_clears_dangling_pointers(client, container, scope):
    """A memory pointing at something that no longer exists is worse than one
    pointing at nothing."""
    await container.memory_service.add(scope, "I live in Chennai.")
    outcome = await container.memory_service.add(scope, "I moved to Bangalore.")
    current_id = outcome.primary.memory_id
    retired_id = outcome.primary.superseded_memory_ids[0]

    await client.delete(
        f"/v1/memories/{retired_id}", params={**scope_params(), "permanent": "true"}
    )

    current = await container.memories.get(scope, current_id)
    assert retired_id not in current.source_memory_ids


async def test_forget_is_idempotent(client, container, scope):
    created = await client.post("/v1/memories/manual", json=body(content="Forget me."))
    memory_id = created.json()["memory"]["memoryId"]

    first = await client.delete(f"/v1/memories/{memory_id}", params=scope_params())
    second = await client.delete(f"/v1/memories/{memory_id}", params=scope_params())

    assert first.status_code == 200
    assert second.status_code == 200
    assert (await container.memories.get(scope, memory_id)).status == MemoryStatus.EXPIRED


async def test_forget_writes_an_audit_row(client, container, scope):
    created = await client.post("/v1/memories/manual", json=body(content="Forget me."))
    await client.delete(
        f"/v1/memories/{created.json()['memory']['memoryId']}", params=scope_params()
    )

    operations = await container.audit.list_for_scope(scope, limit=10)
    assert any(op.action == "EXPIRE" for op in operations)


async def test_cannot_delete_another_tenants_memory(client, container, scope, other_scope):
    created = await client.post("/v1/memories/manual", json=body(content="Mine, not yours."))
    memory_id = created.json()["memory"]["memoryId"]

    res = await client.delete(
        f"/v1/memories/{memory_id}", params=scope_params(client_id="client_intruder")
    )
    assert res.status_code == 404

    # Untouched.
    assert (await container.memories.get(scope, memory_id)).status == MemoryStatus.ACTIVE


# --------------------------------------------------------------------------- #
# Erasing a whole user
# --------------------------------------------------------------------------- #


async def test_purge_requires_explicit_confirmation(client, container, scope):
    await client.post("/v1/memories/manual", json=body(content="Still here."))

    res = await client.delete("/v1/memories", params=scope_params())
    assert res.status_code == 422

    # Nothing removed.
    assert await container.memories.count(scope) >= 1


async def test_purge_erases_everything_for_that_user(client, container, scope):
    await container.memory_service.add(scope, "I live in Chennai.")
    await container.memory_service.add(scope, "I moved to Bangalore.")
    assert await container.memories.count(scope) >= 2

    res = await client.delete("/v1/memories", params={**scope_params(), "confirm": "ERASE"})
    assert res.status_code == 200
    assert res.json()["deletedMemories"] >= 2

    assert await container.memories.count(scope) == 0
    # The audit trail goes too: it would otherwise outlive the data it describes.
    assert await container.audit.list_for_scope(scope, limit=10) == []


async def test_purge_does_not_touch_other_users(client, container, scope, other_scope, store_memory):
    await store_memory("Belongs to the first tenant.", target_scope=scope)
    await store_memory("Belongs to the second tenant.", target_scope=other_scope)

    await client.delete("/v1/memories", params={**scope_params(), "confirm": "ERASE"})

    assert await container.memories.count(scope) == 0
    assert await container.memories.count(other_scope) == 1


async def test_purge_clears_the_vector_index(client, container, scope):
    await client.post("/v1/memories/manual", json=body(content="User plays the sitar."))
    await client.delete("/v1/memories", params={**scope_params(), "confirm": "ERASE"})

    assert await container.vectors.count(scope) == 0


# --------------------------------------------------------------------------- #
# Service-level guard
# --------------------------------------------------------------------------- #


async def test_service_refuses_to_edit_terminal_memories(container, scope):
    await container.memory_service.add(scope, "I live in Chennai.")
    outcome = await container.memory_service.add(scope, "I moved to Bangalore.")
    retired_id = outcome.primary.superseded_memory_ids[0]

    with pytest.raises(InvalidStateTransitionError):
        await container.memory_service.update_manual(
            scope, retired_id, changes={"content": "Rewritten."}
        )
