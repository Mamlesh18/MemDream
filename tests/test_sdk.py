"""SDK tests.

The async client is pointed at the real ASGI app through httpx's ASGI transport,
so these exercise the actual request and response shapes rather than a mock of
them — a serialisation change on the server breaks these tests, which is the
point. (`ASGITransport` is async-only, which is why the sync client is covered
by its local-validation behaviour here and by `examples/` against a live server.)
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402
from memdream import AsyncDreamClient, DreamClient  # noqa: E402
from memdream.exceptions import DreamNotFoundError, DreamValidationError  # noqa: E402

pytestmark = pytest.mark.asyncio

PHONE = "+919999999999"


@pytest.fixture
async def sdk(container):
    app = create_app()
    app.state.container = container

    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Content-Type": "application/json"},
    )
    client = AsyncDreamClient(
        http_client=http,
        client_id="client_test",
        bot_id="bot_test",
        phone_number=PHONE,
    )
    yield client
    await http.aclose()


# --------------------------------------------------------------------------- #
# Basics
# --------------------------------------------------------------------------- #


async def test_health(sdk):
    assert (await sdk.health())["status"] == "ok"


async def test_ready_lists_dependencies(sdk):
    checks = (await sdk.ready())["checks"]
    assert "mongodb" in checks


async def test_add_returns_a_typed_result(sdk):
    result = await sdk.memory.add("I moved to Bangalore.")

    assert result.action in ("ADD", "SUPERSEDE", "MERGE", "NOOP")
    assert result.memory_id
    assert result.extracted_count >= 1
    assert result.remembered is True
    assert result.results[0].reason


async def test_scope_defaults_are_applied(sdk):
    """Scope is set once on the constructor; every call inherits it."""
    result = await sdk.memory.add("I like black coffee.")
    assert result.memory_id
    assert (await sdk.memory.search("coffee")).count >= 1


async def test_search_returns_records_with_scores(sdk):
    await sdk.memory.add("I live in Bangalore.")
    results = await sdk.memory.search("Where does the user live?")

    assert len(results) >= 1
    memory = results[0]
    assert memory.memory_id
    assert memory.content
    assert memory.score is not None
    assert memory.score_breakdown
    assert memory.is_active


async def test_search_result_renders_prompt_context(sdk):
    await sdk.memory.add("I live in Bangalore.")
    context = (await sdk.memory.search("live")).as_context()
    assert context.startswith("- ")


# --------------------------------------------------------------------------- #
# Dream behaviour through the SDK
# --------------------------------------------------------------------------- #


async def test_supersede_is_reported(sdk):
    first = await sdk.memory.add("I live in Chennai.")
    second = await sdk.memory.add("I moved to Bangalore.")

    assert second.action == "SUPERSEDE"
    assert first.memory_id in second.superseded_memory_ids


async def test_get_memory_returns_relationships(sdk):
    await sdk.memory.add("I live in Chennai.")
    second = await sdk.memory.add("I moved to Bangalore.")

    detail = await sdk.memory.get(second.memory_id)
    assert detail.memory.memory_id == second.memory_id
    assert detail.relationships


async def test_history_walks_the_chain(sdk):
    await sdk.memory.add("I live in Chennai.")
    second = await sdk.memory.add("I moved to Bangalore.")

    chain = await sdk.memory.history(second.memory_id)
    assert len(chain) == 2
    assert chain[0].is_active
    assert chain[1].status == "superseded"


async def test_latest_only_filters_history(sdk):
    await sdk.memory.add("I live in Chennai.")
    await sdk.memory.add("I moved to Bangalore.")

    latest = await sdk.memory.search("Where does the user live?", latest_only=True)
    assert latest.count >= 1
    assert all(memory.is_active for memory in latest)


async def test_list_and_audit(sdk):
    await sdk.memory.add("I live in Bangalore.")

    memories = await sdk.memory.list()
    assert memories

    operations = await sdk.memory.audit()
    assert operations
    assert operations[0]["reason"]


async def test_synthesize_returns_a_job(sdk):
    job = await sdk.dream.synthesize()
    assert job.job_id
    assert job.status in ("queued", "running", "completed", "skipped")


async def test_synthesize_wait_returns_a_finished_job(sdk):
    for text in (
        "I prefer morning meetings.",
        "I schedule meetings before 10 AM.",
        "I start work early every day.",
        "I attend early morning calls.",
        "I block my mornings for focused work.",
    ):
        await sdk.memory.add(text)

    job = await sdk.dream.synthesize(wait=True)
    assert job.is_finished
    assert job.memories_analyzed >= 0

    fetched = await sdk.dream.get_job(job.job_id)
    assert fetched.job_id == job.job_id


async def test_dream_stats(sdk):
    await sdk.memory.add("I live in Bangalore.")
    stats = await sdk.dream.stats()
    assert stats["counts"]["active"] >= 1


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


async def test_missing_memory_raises_not_found(sdk):
    with pytest.raises(DreamNotFoundError) as exc:
        await sdk.memory.get("mem_does_not_exist")
    assert exc.value.status_code == 404


async def test_validation_error_is_typed(sdk):
    with pytest.raises(DreamValidationError):
        await sdk.memory.add("")


async def test_cross_tenant_read_raises_not_found(sdk):
    created = await sdk.memory.add("I live in Chennai.")
    with pytest.raises(DreamNotFoundError):
        await sdk.memory.get(created.memory_id, client_id="client_intruder")


# --------------------------------------------------------------------------- #
# Local validation — no request is sent
# --------------------------------------------------------------------------- #


async def test_missing_scope_fails_before_any_request():
    """Caught locally rather than after a round trip to a server that would
    only reject it anyway."""
    client = DreamClient(base_url="http://localhost:1")
    with pytest.raises(ValueError, match="clientId"):
        client.memory.add("hello")
    client.close()


async def test_missing_identity_fails_before_any_request():
    client = DreamClient(base_url="http://localhost:1", client_id="c", bot_id="b")
    with pytest.raises(ValueError, match="phone_number or user_id"):
        client.memory.add("hello")
    client.close()


async def test_sync_and_async_clients_expose_the_same_surface():
    """Parity is easy to break by adding a method to one client only."""
    sync_memory = {name for name in dir(DreamClient(base_url="http://x").memory) if not name.startswith("_")}
    async_memory = {name for name in dir(AsyncDreamClient(base_url="http://x").memory) if not name.startswith("_")}
    assert sync_memory == async_memory
