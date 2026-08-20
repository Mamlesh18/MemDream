"""Flexible scoping: `user_id` required, every other dimension optional.

Forcing `client_id` on a solo developer with one agent is friction for no
benefit. But relaxing it moves the isolation boundary, so the properties below
are the ones that have to hold:

* omitting a dimension is not the same as setting it to empty
* adding a dimension creates a *new* partition rather than widening the old one
* derived identity stays uncorrelated across tenants
"""

from __future__ import annotations

import pytest

from app.core.identity import build_scope, derive_user_id
from app.models.memory import Memory
from app.models.scope import MemoryScope

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# What is required
# --------------------------------------------------------------------------- #


async def test_a_user_id_alone_is_a_valid_scope():
    scope = MemoryScope(user_id="alice")

    assert scope.user_id == "alice"
    assert scope.agent_id is None
    assert scope.client_id is None
    assert scope.partition_key == "user=alice"


async def test_user_id_is_required():
    with pytest.raises(ValueError):
        MemoryScope()


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"user_id": "alice"}, "user=alice"),
        ({"user_id": "alice", "agent_id": "support"}, "user=alice|agent=support"),
        (
            {"user_id": "alice", "agent_id": "support", "client_id": "acme"},
            "user=alice|agent=support|client=acme",
        ),
        (
            {"user_id": "alice", "namespace": {"env": "prod"}},
            "user=alice|env=prod",
        ),
    ],
)
async def test_partition_key_reflects_only_the_dimensions_supplied(kwargs, expected):
    assert MemoryScope(**kwargs).partition_key == expected


async def test_namespace_ordering_does_not_change_the_partition():
    """Two dicts with the same pairs must be the same partition however built."""
    first = MemoryScope(user_id="alice", namespace={"env": "prod", "region": "eu"})
    second = MemoryScope(user_id="alice", namespace={"region": "eu", "env": "prod"})

    assert first.scope_key == second.scope_key


# --------------------------------------------------------------------------- #
# Adding a dimension changes the partition
# --------------------------------------------------------------------------- #


async def test_adding_a_dimension_creates_a_new_partition():
    """Otherwise a deployment that later adds `agent_id` would silently merge
    its new agents into whatever the old unscoped memories were."""
    without = MemoryScope(user_id="alice")
    with_agent = MemoryScope(user_id="alice", agent_id="support")

    assert without.scope_key != with_agent.scope_key


async def test_omitting_a_dimension_differs_from_setting_it_empty():
    """An empty string must not be treated as a distinct value, or callers who
    pass `agent_id=""` would land in a third partition by accident."""
    omitted = build_scope(user_id="alice")
    empty = build_scope(user_id="alice", agent_id="")

    assert omitted.scope_key == empty.scope_key
    assert empty.agent_id is None


async def test_different_agents_are_different_partitions():
    support = MemoryScope(user_id="alice", agent_id="support")
    sales = MemoryScope(user_id="alice", agent_id="sales")

    assert support.scope_key != sales.scope_key


async def test_different_clients_are_different_partitions():
    acme = MemoryScope(user_id="alice", client_id="acme")
    globex = MemoryScope(user_id="alice", client_id="globex")

    assert acme.scope_key != globex.scope_key


# --------------------------------------------------------------------------- #
# Backward compatibility
# --------------------------------------------------------------------------- #


async def test_bot_id_is_accepted_as_the_previous_name_for_agent_id():
    old = MemoryScope(user_id="alice", bot_id="support")
    new = MemoryScope(user_id="alice", agent_id="support")

    assert old.agent_id == "support"
    assert old.bot_id == "support"
    # Critically the same partition, so renaming does not orphan existing data.
    assert old.scope_key == new.scope_key


async def test_memory_accepts_bot_id_too():
    memory = Memory(user_id="alice", bot_id="support", content="A fact.")
    assert memory.agent_id == "support"
    assert memory.bot_id == "support"


# --------------------------------------------------------------------------- #
# Derived identity
# --------------------------------------------------------------------------- #


async def test_derived_ids_stay_uncorrelated_across_clients(settings):
    """The same person contacting two tenants must not be linkable by
    comparing stored ids."""
    acme = derive_user_id(phone_number="+919999999999", client_id="acme", settings=settings)
    globex = derive_user_id(phone_number="+919999999999", client_id="globex", settings=settings)

    assert acme != globex


async def test_derivation_is_deterministic(settings):
    first = derive_user_id(phone_number="+919999999999", client_id="acme", settings=settings)
    second = derive_user_id(phone_number="+919999999999", client_id="acme", settings=settings)
    assert first == second


async def test_a_phone_number_alone_derives_an_id(settings):
    """Single-tenant deployments supply no dimensions and must still work."""
    scope = build_scope(phone_number="+919999999999", settings=settings)

    assert scope.user_id.startswith("user_")
    assert scope.client_id is None
    assert "9999999999" not in scope.user_id


async def test_identity_requires_a_user_id_or_a_phone_number(settings):
    with pytest.raises(Exception, match="userId"):
        build_scope(agent_id="support", settings=settings)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


async def test_memory_derives_its_own_scope_key():
    """Derived on the model so no construction site can forget it and write a
    row that its own partition filter would never find."""
    memory = Memory(user_id="alice", agent_id="support", content="A fact.")

    assert memory.scope_key
    assert memory.scope_key == MemoryScope(user_id="alice", agent_id="support").scope_key


async def test_the_filter_is_a_single_indexed_key():
    """One equality check rather than a predicate over optional fields — the
    latter has to reason about null-versus-missing per dimension, and getting
    that wrong matches the wrong partition."""
    scope = MemoryScope(user_id="alice", agent_id="support")
    assert scope.as_filter() == {"scopeKey": scope.scope_key}


async def test_readable_dimensions_are_persisted_alongside_the_key():
    """Filtering never uses them, but an operator querying by hand needs them."""
    scope = MemoryScope(user_id="alice", agent_id="support", client_id="acme")
    fields = scope.as_document_fields()

    assert fields["userId"] == "alice"
    assert fields["agentId"] == "support"
    assert fields["clientId"] == "acme"
    assert fields["scopeKey"] == scope.scope_key


async def test_scope_matching_falls_back_for_rows_written_before_scope_key():
    """A deployment mid-migration must read its own history rather than
    reporting an isolation violation."""
    scope = MemoryScope(user_id="alice", agent_id="support")
    legacy = {"userId": "alice", "agentId": "support", "clientId": None}

    assert scope.matches(legacy) is True
    assert scope.matches({"userId": "bob", "agentId": "support", "clientId": None}) is False


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #


async def test_memories_work_with_only_a_user_id(container, store_memory):
    scope = MemoryScope(user_id="alice")
    await store_memory("User likes filter coffee.", target_scope=scope)

    found = await container.memories.find(scope, limit=10)
    assert len(found) == 1
    assert found[0].content == "User likes filter coffee."


async def test_agents_are_isolated_from_each_other(container, store_memory):
    support = MemoryScope(user_id="alice", agent_id="support")
    sales = MemoryScope(user_id="alice", agent_id="sales")

    await store_memory("Raised a support ticket.", target_scope=support)
    await store_memory("Asked about pricing.", target_scope=sales)

    assert len(await container.memories.find(support, limit=10)) == 1
    assert len(await container.memories.find(sales, limit=10)) == 1
    assert (await container.memories.find(support, limit=10))[0].content.startswith("Raised")


async def test_an_unscoped_user_cannot_see_a_scoped_one(container, store_memory):
    """Adding a dimension partitions; it does not widen."""
    plain = MemoryScope(user_id="alice")
    scoped = MemoryScope(user_id="alice", agent_id="support")

    await store_memory("Belongs to the agent.", target_scope=scoped)

    assert await container.memories.find(plain, limit=10) == []


async def test_namespace_dimensions_isolate(container, store_memory):
    prod = MemoryScope(user_id="alice", namespace={"env": "prod"})
    staging = MemoryScope(user_id="alice", namespace={"env": "staging"})

    await store_memory("Production fact.", target_scope=prod)

    assert len(await container.memories.find(prod, limit=10)) == 1
    assert await container.memories.find(staging, limit=10) == []
