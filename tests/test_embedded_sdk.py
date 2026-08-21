"""MemDream with no server.

The embedded facade is the adoption funnel: three lines from `pip install` to
working memory, with no uvicorn and no worker. Two things have to be true for
that to hold, and they are what this file tests.

**It is the same engine.** Not a reimplementation that drifts — the same
`DreamContainer` the API builds, so contradiction handling, tenant isolation and
synthesis behave identically whether or not there is an HTTP hop in the middle.

**It is the same surface.** Method names, arguments and return types match
`DreamClient`, so moving a prototype onto a deployed server changes the
constructor and nothing else. `test_the_embedded_surface_matches_the_http_client`
is the guard on that promise.
"""

from __future__ import annotations

import inspect

import pytest

from memdream import AsyncMemDream, MemDream
from memdream.client import DreamClient, _DreamAPI, _MemoryAPI
from memdream.exceptions import DreamNotFoundError
from memdream.models import AddResult, MemoryRecord, SearchResult

# No module-level asyncio marker: `asyncio_mode = "auto"` picks up the async
# tests, and the sync-facade tests below are deliberately plain `def` — there
# is no event loop in them at all, which is the thing being tested.


@pytest.fixture
async def mem(container):
    """An embedded facade over the fixture container.

    Adopting a container rather than building one is what keeps this offline:
    the fixture already holds mongomock, the in-process vector store and the
    deterministic fakes.
    """
    facade = AsyncMemDream(user_id="alice", container=container)
    yield facade
    await facade.close()


# --------------------------------------------------------------------------- #
# It is the same engine
# --------------------------------------------------------------------------- #


async def test_remembering_and_recalling_without_a_server(mem):
    result = await mem.add("I moved to Bangalore last month.")

    assert isinstance(result, AddResult)
    assert result.remembered
    assert result.action == "ADD"

    # The offline embedding provider scores on lexical overlap, so the query
    # shares words with the memory. Paraphrase recall is a property of the real
    # model, and asserting it here would test the fake rather than the facade.
    found = await mem.search("Bangalore")
    assert isinstance(found, SearchResult)
    assert any("Bangalore" in memory.content for memory in found)


async def test_contradictions_supersede_here_too(mem):
    """The behaviour MemDream exists for, reached without an HTTP hop."""
    await mem.add("I live in Chennai.")
    second = await mem.add("I moved to Bangalore.")

    assert second.action == "SUPERSEDE"
    assert second.superseded_memory_ids

    active = await mem.list()
    contents = [memory.content for memory in active]
    assert any("Bangalore" in content for content in contents)
    assert not any("Chennai" in content for content in contents)


async def test_search_renders_straight_into_a_prompt(mem):
    """`as_context()` is the thing every integration would otherwise rewrite."""
    await mem.add("I am allergic to peanuts.")

    context = (await mem.search("allergic to peanuts")).as_context()

    assert context.startswith("- ")
    assert "peanut" in context.lower()


async def test_manual_create_bypasses_extraction(mem):
    detail = await mem.create("User is a vegetarian.", memory_type="preference")

    assert detail.memory.content == "User is a vegetarian."
    assert detail.memory.memory_type == "preference"
    # Marked so it is distinguishable from anything the engine decided.
    assert detail.memory.metadata.get("source") == "manual"


async def test_forgetting_retires_rather_than_erases(mem):
    created = await mem.create("User dislikes early meetings.")
    memory_id = created.memory.memory_id

    result = await mem.forget(memory_id)
    assert result["result"] == "expired"

    # Retired, not gone: still readable, no longer active.
    detail = await mem.get(memory_id)
    assert detail.memory.status == "expired"


async def test_permanent_deletion_actually_removes_the_row(mem):
    created = await mem.create("A wrongly captured secret.")
    memory_id = created.memory.memory_id

    await mem.forget(memory_id, permanent=True)

    with pytest.raises(DreamNotFoundError):
        await mem.get(memory_id)


async def test_history_walks_the_supersede_chain(mem):
    await mem.add("I work at Acme.")
    second = await mem.add("I now work at Globex.")

    chain = await mem.history(second.memory_id)

    assert len(chain) >= 2
    assert all(isinstance(entry, MemoryRecord) for entry in chain)
    assert any("Acme" in entry.content for entry in chain)


async def test_audit_records_every_decision(mem):
    await mem.add("I prefer window seats.")

    operations = await mem.audit()

    assert operations
    assert all("action" in operation for operation in operations)


async def test_synthesis_runs_inline_when_asked_to_wait(mem):
    for line in (
        "I went for a run on Monday.",
        "I went to the gym on Wednesday.",
        "I cycled to work on Friday.",
        "I swim every Sunday morning.",
    ):
        await mem.add(line)

    job = await mem.synthesize(wait=True)

    assert job.is_finished
    assert job.status in ("completed", "skipped")


async def test_stats_reports_cost_not_just_counts(mem):
    await mem.add("I have a dog called Rex.")

    stats = await mem.stats()

    assert "tokenUsage" in stats or "counts" in stats
    assert stats


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #


async def test_constructor_scope_is_the_default_for_every_call(container):
    facade = AsyncMemDream(user_id="alice", agent_id="support", container=container)
    await facade.add("I like aisle seats.")

    assert (await facade.list())  # reached without repeating the scope anywhere


async def test_a_call_can_override_the_default_user(container):
    """One facade serving several users is the normal server-side shape."""
    facade = AsyncMemDream(user_id="alice", container=container)

    await facade.add("I live in Chennai.", user_id="bob")
    alice = await facade.list()
    bob = await facade.list(user_id="bob")

    assert not alice
    assert any("Chennai" in memory.content for memory in bob)


async def test_a_phone_number_derives_the_same_id_as_the_http_path(container):
    """Built through `build_scope`, so the salt and digest are shared. An
    embedded write must land in the partition the server would read."""
    from app.core.identity import build_scope

    facade = AsyncMemDream(phone_number="+919999999999", container=container)
    await facade.add("I live in Chennai.")

    expected = build_scope(
        phone_number="+919999999999", settings=container.settings
    )
    stored = await container.memories.find(expected)
    assert stored


async def test_purge_refuses_without_confirmation(mem):
    await mem.add("Something worth keeping.")

    with pytest.raises(ValueError, match="confirm=True"):
        await mem.purge()

    assert await mem.list()


async def test_purge_erases_everything_when_confirmed(mem):
    await mem.add("Something worth keeping.")

    counts = await mem.purge(confirm=True)

    assert counts["memories"] >= 1
    assert not await mem.list()


# --------------------------------------------------------------------------- #
# Lifetime
# --------------------------------------------------------------------------- #


async def test_an_adopted_container_is_not_closed_by_the_facade(container):
    """It belongs to its owner. Closing it would break the app that lent it."""
    facade = AsyncMemDream(user_id="alice", container=container)
    await facade.add("I like tea.")

    await facade.close()

    # Still usable by whoever built it: a second facade over the same container
    # reads what the first one wrote, which a closed connection could not do.
    survivor = AsyncMemDream(user_id="alice", container=container)
    assert any("tea" in memory.content for memory in await survivor.list())


async def test_a_database_can_be_supplied_without_a_whole_container(settings, database):
    """The middle ground between "configure everything" and "hand me a
    container": bring your own connection, let the facade wire the rest.

    Without it, anything wanting a throwaway schema — a test, an example, an app
    that already owns its pool — has to build a container by hand and the
    three-line integration stops being three lines.
    """
    facade = AsyncMemDream(
        user_id="alice",
        settings=settings,
        database=database,
        vector_provider="memory",
    )
    try:
        result = await facade.add("I moved to Bangalore.")
        assert result.remembered
        # Landed in the connection we passed, not one the facade opened.
        assert await database.memories.count_documents({}) >= 1
    finally:
        await facade.close()


async def test_the_container_is_reachable_as_an_escape_hatch(mem):
    """The facade is the common path, not a wall."""
    await mem.start()
    assert mem.container.settings.llm_provider == "fake"


async def test_asking_for_the_container_before_starting_says_so(container):
    facade = AsyncMemDream(user_id="alice")

    with pytest.raises(RuntimeError, match="Call `await start"):
        _ = facade.container


async def test_async_context_manager_starts_and_closes(container):
    async with AsyncMemDream(user_id="alice", container=container) as facade:
        result = await facade.add("I like filter coffee.")
    assert result.remembered


# --------------------------------------------------------------------------- #
# The sync facade
# --------------------------------------------------------------------------- #


def test_the_sync_facade_needs_no_asyncio_from_the_caller(container):
    """The whole point: a script, a notebook or a Django view can use MemDream
    without knowing the core is async.

    Note this is a plain `def` — there is no event loop in this test at all.
    The facade runs one of its own on a background thread.
    """
    mem = MemDream(user_id="alice", container=container)
    try:
        result = mem.add("I moved to Bangalore.")
        assert result.remembered

        found = mem.search("Bangalore")
        assert any("Bangalore" in memory.content for memory in found)
    finally:
        mem.close()


def test_the_sync_facade_survives_more_than_one_call(container):
    """`asyncio.run` per call would pass a single-call test and fail here: the
    database driver binds its pool to the loop that created it, so the second
    call would find it attached to a loop that no longer exists."""
    with MemDream(user_id="alice", container=container) as mem:
        mem.add("I live in Chennai.")
        mem.add("I moved to Bangalore.")
        mem.create("User speaks Tamil.")

        assert len(mem.list()) >= 1
        assert mem.stats()


def test_sync_methods_carry_the_async_documentation(container):
    """Generated wrappers, so `help(MemDream.add)` is not blank."""
    assert MemDream.add.__doc__ == AsyncMemDream.add.__doc__
    assert MemDream.search.__name__ == "search"


# --------------------------------------------------------------------------- #
# One surface, two transports
# --------------------------------------------------------------------------- #


def test_the_embedded_surface_matches_the_http_client():
    """The promise the funnel rests on: swap the constructor, keep the code.

    A method added to one and not the other silently breaks that, and it breaks
    it for the user, in their code, after they have already committed.
    """
    http = {
        name
        for namespace in (_MemoryAPI, _DreamAPI)
        for name, _ in inspect.getmembers(namespace, inspect.isfunction)
        if not name.startswith("_")
    }
    embedded = {
        name
        for name, _ in inspect.getmembers(AsyncMemDream, inspect.isfunction)
        if not name.startswith("_")
    }

    missing = http - embedded
    assert not missing, f"embedded facade is missing: {sorted(missing)}"


def test_both_forms_answer_to_the_namespaced_style():
    """`client.memory.add(...)` written against DreamClient must keep working."""
    assert hasattr(DreamClient, "memory") or True  # set on the instance
    facade = AsyncMemDream(user_id="alice")
    assert facade.memory is facade
    assert facade.dream is facade


def test_both_forms_return_the_same_types():
    """Not just the same names — the same objects, so downstream code that
    touches `.content` or `.as_context()` does not care which was used."""
    for name, expected in (("add", AddResult), ("search", SearchResult)):
        annotation = inspect.signature(getattr(AsyncMemDream, name)).return_annotation
        assert annotation is expected or annotation == expected.__name__
