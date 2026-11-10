"""MemDream as a library: nothing to run but your own code.

Three promises the README makes to someone adding MemDream to their project:

* `database="memory"` works straight after install — no database server;
* `delete(id)` erases a memory, `forget(id)` only retires it;
* Dream runs on its own (`auto_dream=True`, the default), including the daily
  pass that otherwise only the API server started — and `auto_dream=False`
  turns all of it off.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from memdream import AsyncMemDream, MemDream
from memdream.exceptions import DreamNotFoundError

PROMPTS = str(Path(__file__).resolve().parents[1] / "prompts")

OFFLINE = {
    "prompts": PROMPTS,
    "llm_provider": "fake",
    "embedding_provider": "fake",
    "identity_salt": "test-salt-do-not-use-in-production",
    "log_level": "WARNING",
    "log_json": False,
}


def test_in_memory_database_needs_no_server():
    with MemDream(user_id="alice", database="memory", **OFFLINE) as mem:
        added = mem.add("I live in Chennai.", raw=True)
        assert added.action == "ADD"

        # The vector index followed the database into memory: nothing on disk
        # that would outlive the rows it indexes.
        assert mem.container.settings.vector_provider == "memory"

        assert any("Chennai" in m.content for m in mem.list())


def test_delete_erases_and_forget_retires():
    with MemDream(user_id="alice", database="memory", **OFFLINE) as mem:
        kept = mem.create("User is allergic to peanuts.")
        gone = mem.create("User has a dog named Bruno.")

        retired = mem.forget(kept.memory.memory_id)
        assert retired["result"] == "expired"
        # Retired, not erased: still readable.
        assert mem.get(kept.memory.memory_id).memory.status == "expired"

        erased = mem.delete(gone.memory.memory_id)
        assert erased == {
            "memoryId": gone.memory.memory_id,
            "result": "deleted",
            "permanent": True,
        }
        with pytest.raises(DreamNotFoundError):
            mem.get(gone.memory.memory_id)


async def test_auto_dream_starts_the_daily_pass_and_close_stops_it():
    mem = AsyncMemDream(user_id="alice", database="memory", **OFFLINE)
    container = await mem.start()

    scheduler = container.scheduler
    assert scheduler is not None
    assert scheduler._task is not None and not scheduler._task.done()

    await mem.close()
    assert scheduler._task is None


async def test_auto_dream_off_runs_nothing_by_itself():
    mem = AsyncMemDream(user_id="alice", database="memory", auto_dream=False, **OFFLINE)
    container = await mem.start()
    try:
        assert container.scheduler is None

        calls = []

        async def spy(scope):
            calls.append(scope)

        container.dream_service.maybe_trigger = spy
        await mem.add("I live in Chennai.", raw=True)
        assert calls == []
    finally:
        await mem.close()


async def test_an_adopted_container_gets_no_scheduler_from_the_facade(container):
    """The owner of an adopted container (the API, a test) decides its clock."""
    mem = AsyncMemDream(user_id="alice", container=container)
    await mem.start()
    assert container.scheduler is None
    await mem.close()


def test_delete_leaves_no_trace_of_the_content_in_the_audit_log():
    """An erasure request is not met while the audit log still holds the text."""
    with MemDream(user_id="alice", database="memory", **OFFLINE) as mem:
        added = mem.add("I live in Chennai.", raw=True)
        kept = mem.add("I am allergic to peanuts.", raw=True)

        mem.delete(added.memory_id)

        rows = mem.audit()
        assert rows, "the decisions are still recorded"
        dumped = repr(rows)
        assert "Chennai" not in dumped
        # Only rows about the erased memory are redacted.
        assert "peanuts" in dumped

        delete_rows = [row for row in rows if row["action"] == "DELETE"]
        assert delete_rows and delete_rows[0]["inputContent"] is None
        assert kept.memory_id


# --------------------------------------------------------------------------- #
# The same promises on PostgreSQL as the canonical store
# --------------------------------------------------------------------------- #


@pytest.fixture
def postgres():
    """Library arguments for a real PostgreSQL, or skip.

    Everything else in the suite runs on the Mongo repositories; without this,
    the Postgres memories, audit and job repositories had no test at all.
    """
    dsn = os.environ.get("DREAM_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set DREAM_TEST_POSTGRES_DSN to run the library against PostgreSQL")
    pytest.importorskip("asyncpg", reason="asyncpg not installed")
    yield {**OFFLINE, "database": dsn, "vector": "memory"}
    _drop_postgres_tables(dsn)


def _drop_postgres_tables(dsn: str) -> None:
    import asyncio

    import asyncpg

    from memdream.engine.repositories.postgres.database import (
        JOBS,
        MEMORIES,
        OPERATIONS,
        RELATIONS,
    )

    async def drop() -> None:
        connection = await asyncpg.connect(dsn)
        try:
            for table in (RELATIONS, OPERATIONS, JOBS, MEMORIES):
                await connection.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        finally:
            await connection.close()

    asyncio.run(drop())


def test_postgres_remembers_supersedes_and_recalls(postgres):
    with MemDream(user_id="alice", **postgres) as mem:
        first = mem.add("I live in Chennai.")
        second = mem.add("I moved to Bangalore.")
        assert first.action == "ADD"
        assert second.action == "SUPERSEDE"
        assert second.superseded_memory_ids == [first.memory_id]

        active = [m.content for m in mem.list()]
        assert any("Bangalore" in c for c in active)
        assert not any("Chennai" in c for c in active)
        # History survives on Postgres too.
        assert [m.status for m in mem.history(second.memory_id)] == ["active", "superseded"]


def test_postgres_delete_redacts_the_audit_log(postgres):
    with MemDream(user_id="alice", **postgres) as mem:
        gone = mem.add("I live in Chennai.", raw=True)
        mem.add("I am allergic to peanuts.", raw=True)

        mem.delete(gone.memory_id)

        dumped = repr(mem.audit())
        assert "Chennai" not in dumped
        assert "peanuts" in dumped
        with pytest.raises(DreamNotFoundError):
            mem.get(gone.memory_id)


def test_postgres_isolates_users(postgres):
    with MemDream(user_id="alice", **postgres) as mem:
        mem.create("User is allergic to peanuts.")
        assert mem.list(user_id="bob") == []
        assert len(mem.list()) == 1
