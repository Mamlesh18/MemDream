"""Upgrading a database that already has data in it.

Both halves of this were found the hard way, by a real deployment failing to
start after the scope refactor. They are two separate failures with the same
cause, and only one of them is loud:

**The schema.** `scope_status` used to index `(clientId, botId, userId, status)`
and now indexes `(scopeKey, status)`. MongoDB refuses to reuse an index name for
a different key, so the service crashed on boot with `IndexKeySpecsConflict`.

**The data.** Documents written before `scopeKey` existed do not have one, and
every scoped query filters on it. Nothing errors — a user with years of history
simply looks brand new. That silence is why the migration reports what it finds
rather than only what it fixed.

Index reconciliation is exercised only against a real MongoDB: mongomock accepts
conflicting index definitions without complaint, so asserting against it would
prove the opposite of what is wanted. The data migration runs everywhere.
"""

from __future__ import annotations

import os

import pytest
from pymongo import ASCENDING, IndexModel

from app.models.scope import MemoryScope
from app.repositories.mongo.database import _normalise_key, _sync_indexes
from scripts.migrate_scope_key import migrate_collection, scope_key_for

requires_real_mongo = pytest.mark.skipif(
    not os.environ.get("DREAM_TEST_MONGODB_URI"),
    reason="index conflicts need a real MongoDB; mongomock does not enforce them",
)


# --------------------------------------------------------------------------- #
# The data: computing the key
# --------------------------------------------------------------------------- #


def test_the_key_matches_what_the_scope_would_compute():
    """The migration and the running service must agree, or the backfill writes
    a partition nothing will ever query."""
    document = {"userId": "alice", "agentId": "support", "clientId": "acme"}

    assert scope_key_for(document) == MemoryScope(
        user_id="alice", agent_id="support", client_id="acme"
    ).scope_key


def test_a_pre_rename_document_keys_on_its_bot_id():
    """`botId` became `agentId`, and the digest is over the new spelling. Reading
    only `agentId` here would put every pre-rename row in the wrong partition —
    silently, since a wrong digest is still a valid digest."""
    old = {"userId": "alice", "botId": "support"}
    new = {"userId": "alice", "agentId": "support"}

    assert scope_key_for(old) == scope_key_for(new)


def test_agent_id_wins_when_a_document_carries_both():
    """Mid-migration rows can have both. The new spelling is the truth."""
    document = {"userId": "alice", "agentId": "support", "botId": "stale"}

    assert scope_key_for(document) == MemoryScope(
        user_id="alice", agent_id="support"
    ).scope_key


def test_the_namespace_is_part_of_the_key():
    """Omitting it would merge two partitions that the service keeps apart."""
    plain = {"userId": "alice"}
    scoped = {"userId": "alice", "namespace": {"env": "prod"}}

    assert scope_key_for(plain) != scope_key_for(scoped)
    assert scope_key_for(scoped) == MemoryScope(
        user_id="alice", namespace={"env": "prod"}
    ).scope_key


def test_a_document_with_no_user_is_refused_rather_than_guessed():
    """`userId` is the isolation boundary. A row without one belongs to no
    partition, and inventing one would put it in somebody's."""
    assert scope_key_for({"clientId": "acme", "botId": "support"}) is None


# --------------------------------------------------------------------------- #
# The data: the backfill itself
# --------------------------------------------------------------------------- #


@pytest.fixture
async def legacy(database):
    """Four documents as a pre-scopeKey deployment would have written them."""
    await database.memories.insert_many(
        [
            {"_id": "mem_1", "userId": "alice", "botId": "support", "clientId": "acme",
             "content": "User lives in Chennai."},
            {"_id": "mem_2", "userId": "alice", "botId": "support", "clientId": "acme",
             "content": "User is allergic to peanuts."},
            {"_id": "mem_3", "userId": "bob", "clientId": "acme", "content": "User is vegan."},
            # Already migrated: must be left exactly as it is.
            {"_id": "mem_4", "userId": "carol", "scopeKey": "already-set",
             "content": "User cycles to work."},
        ]
    )
    return database


async def test_the_backfill_makes_invisible_memories_reachable(legacy):
    """The whole point. Before: a scoped query matches nothing."""
    scope = MemoryScope(user_id="alice", agent_id="support", client_id="acme")

    assert await legacy.memories.count_documents(scope.as_filter()) == 0

    await migrate_collection(legacy, "memories", dry_run=False, batch_size=500)

    assert await legacy.memories.count_documents(scope.as_filter()) == 2


async def test_the_backfill_writes_the_new_spelling_too(legacy):
    """So the row stops needing the botId compatibility path on every read."""
    await migrate_collection(legacy, "memories", dry_run=False, batch_size=500)

    document = await legacy.memories.find_one({"_id": "mem_1"})
    assert document["agentId"] == "support"
    # The old field is left in place: removing it is not this migration's job,
    # and a rollback would need it.
    assert document["botId"] == "support"


async def test_an_already_migrated_document_is_untouched(legacy):
    await migrate_collection(legacy, "memories", dry_run=False, batch_size=500)

    document = await legacy.memories.find_one({"_id": "mem_4"})
    assert document["scopeKey"] == "already-set"


async def test_a_dry_run_writes_nothing(legacy):
    stats = await migrate_collection(legacy, "memories", dry_run=True, batch_size=500)

    assert stats["pending"] == 3
    assert stats["updated"] == 3, "a dry run still reports what it would do"

    unchanged = await legacy.memories.find_one({"_id": "mem_1"})
    assert "scopeKey" not in unchanged


async def test_running_it_twice_changes_nothing_the_second_time(legacy):
    """Deterministic key, so a re-run over the same data is a no-op. Anyone
    interrupted halfway needs to be able to just run it again."""
    first = await migrate_collection(legacy, "memories", dry_run=False, batch_size=500)
    second = await migrate_collection(legacy, "memories", dry_run=False, batch_size=500)

    assert first["updated"] == 3
    assert second["pending"] == 0
    assert second["updated"] == 0


async def test_batching_does_not_lose_documents(legacy):
    """A batch size smaller than the backlog exercises the flush-mid-cursor
    path, where an off-by-one silently drops the tail."""
    stats = await migrate_collection(legacy, "memories", dry_run=False, batch_size=2)

    assert stats["updated"] == 3
    assert await legacy.memories.count_documents({"scopeKey": {"$exists": True}}) == 4


async def test_every_scoped_collection_is_covered(database):
    """A memory whose audit rows were left behind has a history that stops."""
    from scripts.migrate_scope_key import SCOPED_COLLECTIONS

    assert set(SCOPED_COLLECTIONS) == {
        "memories",
        "memory_relations",
        "dream_operations",
        "dream_jobs",
    }


# --------------------------------------------------------------------------- #
# The schema
# --------------------------------------------------------------------------- #


def test_key_comparison_survives_the_two_shapes_mongo_returns():
    """`IndexModel.document["key"]` is a SON, `index_information()` gives a list
    of tuples. Comparing them directly would report every index as changed and
    rebuild the lot on every boot."""
    model = IndexModel([("scopeKey", ASCENDING), ("status", ASCENDING)], name="scope_status")

    assert _normalise_key(model.document["key"]) == [("scopeKey", 1), ("status", 1)]
    assert _normalise_key([("scopeKey", 1), ("status", 1)]) == [("scopeKey", 1), ("status", 1)]


def test_field_order_is_part_of_the_comparison():
    """A compound index on (a, b) is not the index on (b, a), and treating them
    as equal would leave the wrong one in place."""
    assert _normalise_key([("a", 1), ("b", 1)]) != _normalise_key([("b", 1), ("a", 1)])


@requires_real_mongo
async def test_a_renamed_index_definition_is_replaced_not_fatal(database):
    """The failure that stopped a real deployment booting."""
    await database.memories.create_indexes(
        [
            IndexModel(
                [("clientId", ASCENDING), ("botId", ASCENDING), ("status", ASCENDING)],
                name="scope_status",
            )
        ]
    )

    # Same name, different key: MongoDB would refuse this on its own.
    await _sync_indexes(
        database.memories,
        [IndexModel([("scopeKey", ASCENDING), ("status", ASCENDING)], name="scope_status")],
    )

    information = await database.memories.index_information()
    assert _normalise_key(information["scope_status"]["key"]) == [("scopeKey", 1), ("status", 1)]


@requires_real_mongo
async def test_an_unchanged_index_is_not_rebuilt(database):
    """Rebuilding a large index on every boot would be a self-inflicted outage."""
    models = [IndexModel([("scopeKey", ASCENDING), ("status", ASCENDING)], name="scope_status")]

    await _sync_indexes(database.memories, models)
    before = (await database.memories.index_information())["scope_status"]
    await _sync_indexes(database.memories, models)
    after = (await database.memories.index_information())["scope_status"]

    assert before == after


@requires_real_mongo
async def test_indexes_we_did_not_declare_are_left_alone(database):
    """Only MemDream's own names are touched. An index an operator added for
    their own reporting must survive an upgrade."""
    await database.memories.create_indexes(
        [IndexModel([("content", ASCENDING)], name="operators_own_index")]
    )

    await database.ensure_schema()

    assert "operators_own_index" in await database.memories.index_information()
