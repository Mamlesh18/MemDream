"""One contract, run against every vector store.

Adding a backend is where tenant isolation is most likely to break: each store
partitions differently — Pinecone has namespaces, Qdrant has payload filters,
pgvector has WHERE clauses — and it is easy to implement one of them subtly
wrong in a way that only shows up as one customer seeing another's memories.

So the guarantees are written once, here, and every implementation runs the same
suite. A store that does not pass this is not safe to ship, whatever else it does.

Stores whose driver or server is unavailable are skipped, not failed, so the
suite stays useful on a laptop with nothing installed. Run the full matrix with:

    docker compose -f docker-compose.test.yml up -d
    pytest tests/test_vector_store_conformance.py
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import AsyncIterator

import pytest

from app.models.scope import MemoryScope
from app.providers.vector_store.base import VectorRecord, VectorStore

pytestmark = pytest.mark.asyncio

DIM = 8


def _vec(*values: float) -> list[float]:
    """A DIM-length vector padded with zeros, so tests can name a few axes."""
    padded = list(values) + [0.0] * (DIM - len(values))
    return padded[:DIM]


A = _vec(1.0, 0.0, 0.0)     # a direction
B = _vec(0.0, 1.0, 0.0)     # orthogonal to A
A_NEAR = _vec(0.95, 0.31, 0.0)  # close to A


def scope_of(client: str = "acme", bot: str = "support", user: str = "u1") -> MemoryScope:
    return MemoryScope(client_id=client, bot_id=bot, user_id=user)


# --------------------------------------------------------------------------- #
# The store matrix
#
# Each entry yields a ready store, or skips when its dependency is absent.
# --------------------------------------------------------------------------- #


async def _memory_store() -> AsyncIterator[VectorStore]:
    from app.providers.vector_store.memory_store import InMemoryVectorStore

    store = InMemoryVectorStore()
    yield store
    await store.close()


async def _faiss_store() -> AsyncIterator[VectorStore]:
    pytest.importorskip("faiss", reason="faiss-cpu not installed")
    from app.providers.vector_store.faiss_store import FaissVectorStore

    with tempfile.TemporaryDirectory() as directory:
        store = FaissVectorStore(path=directory, dimensions=DIM)
        yield store
        await store.close()


async def _qdrant_store() -> AsyncIterator[VectorStore]:
    pytest.importorskip("qdrant_client", reason="qdrant-client not installed")
    url = os.environ.get("DREAM_TEST_QDRANT_URL")
    if not url:
        pytest.skip("set DREAM_TEST_QDRANT_URL to run the Qdrant conformance suite")

    from app.providers.vector_store.qdrant_store import QdrantVectorStore

    store = QdrantVectorStore(url=url, collection="memdream_conformance", dimensions=DIM)
    yield store
    await store.close()


async def _pgvector_store() -> AsyncIterator[VectorStore]:
    pytest.importorskip("asyncpg", reason="asyncpg not installed")
    dsn = os.environ.get("DREAM_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set DREAM_TEST_POSTGRES_DSN to run the pgvector conformance suite")

    from app.providers.vector_store.pgvector_store import PgVectorStore

    store = PgVectorStore(dsn=dsn, table="memdream_conformance", dimensions=DIM)
    yield store
    await store.close()


async def _chroma_store() -> AsyncIterator[VectorStore]:
    pytest.importorskip("chromadb", reason="chromadb not installed")
    from app.providers.vector_store.chroma_store import ChromaVectorStore

    # ignore_cleanup_errors: Chroma keeps its SQLite file open, and on Windows
    # that blocks removal of the temp directory. A harness detail, not a defect.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        store = ChromaVectorStore(path=directory, collection="memdream_conformance")
        yield store
        await store.close()


async def _pinecone_store() -> AsyncIterator[VectorStore]:
    pytest.importorskip("pinecone", reason="pinecone client not installed")
    key = os.environ.get("DREAM_TEST_PINECONE_API_KEY")
    index = os.environ.get("DREAM_TEST_PINECONE_INDEX")
    if not (key and index):
        pytest.skip("set DREAM_TEST_PINECONE_API_KEY and DREAM_TEST_PINECONE_INDEX")

    from app.providers.vector_store.pinecone_store import PineconeVectorStore

    store = PineconeVectorStore(api_key=key, index_name=index, dimensions=DIM)
    yield store
    await store.close()


STORES = {
    "memory": _memory_store,
    "faiss": _faiss_store,
    "qdrant": _qdrant_store,
    "pgvector": _pgvector_store,
    "chroma": _chroma_store,
    "pinecone": _pinecone_store,
}


@pytest.fixture(params=list(STORES), ids=list(STORES))
async def store(request) -> AsyncIterator[VectorStore]:
    async for instance in STORES[request.param]():
        # Start from a clean slate — a shared server keeps state between runs.
        for scope in (
            scope_of(),
            scope_of(client="other"),
            scope_of(client="globex"),
            scope_of(bot="sales"),
            scope_of(user="u2"),
        ):
            with contextlib.suppress(NotImplementedError):
                await instance.clear(scope)
        yield instance


# --------------------------------------------------------------------------- #
# Basic round-trip
# --------------------------------------------------------------------------- #


async def test_upsert_then_search_returns_the_vector(store):
    scope = scope_of()
    await store.upsert(scope, [VectorRecord("mem_1", A)])

    hits = await store.search(scope, A, top_k=5)

    assert [hit.memory_id for hit in hits] == ["mem_1"]
    assert hits[0].score == pytest.approx(1.0, abs=0.01), "identical vectors score ~1.0"


async def test_search_orders_by_similarity(store):
    scope = scope_of()
    await store.upsert(scope, [VectorRecord("near", A_NEAR), VectorRecord("far", B)])

    hits = await store.search(scope, A, top_k=5)

    assert hits[0].memory_id == "near"
    assert hits[0].score > hits[-1].score


async def test_search_respects_top_k(store):
    scope = scope_of()
    await store.upsert(scope, [VectorRecord(f"mem_{i}", A_NEAR) for i in range(5)])

    assert len(await store.search(scope, A, top_k=2)) <= 2


async def test_empty_store_returns_no_hits(store):
    assert await store.search(scope_of(), A, top_k=5) == []


async def test_count_reflects_what_was_stored(store):
    scope = scope_of()
    assert await store.count(scope) == 0

    await store.upsert(scope, [VectorRecord("mem_1", A), VectorRecord("mem_2", B)])
    assert await store.count(scope) == 2


# --------------------------------------------------------------------------- #
# Upsert semantics
# --------------------------------------------------------------------------- #


async def test_upserting_the_same_id_replaces_rather_than_duplicates(store):
    """A memory edited in place must not leave its old vector behind, or search
    returns the same memory twice with contradictory content."""
    scope = scope_of()
    await store.upsert(scope, [VectorRecord("mem_1", A)])
    await store.upsert(scope, [VectorRecord("mem_1", B)])

    assert await store.count(scope) == 1

    hits = await store.search(scope, B, top_k=5)
    assert hits[0].memory_id == "mem_1"
    assert hits[0].score == pytest.approx(1.0, abs=0.01), "the new vector must win"


async def test_update_behaves_like_upsert(store):
    scope = scope_of()
    await store.upsert(scope, [VectorRecord("mem_1", A)])
    await store.update(scope, [VectorRecord("mem_1", B)])

    assert await store.count(scope) == 1
    assert (await store.search(scope, B, top_k=1))[0].score == pytest.approx(1.0, abs=0.01)


async def test_empty_upsert_is_a_no_op(store):
    await store.upsert(scope_of(), [])
    assert await store.count(scope_of()) == 0


# --------------------------------------------------------------------------- #
# Deletion
# --------------------------------------------------------------------------- #


async def test_delete_removes_from_search(store):
    scope = scope_of()
    await store.upsert(scope, [VectorRecord("mem_1", A), VectorRecord("mem_2", B)])

    await store.delete(scope, ["mem_1"])

    assert await store.count(scope) == 1
    assert "mem_1" not in {hit.memory_id for hit in await store.search(scope, A, top_k=5)}


async def test_deleting_an_unknown_id_is_not_an_error(store):
    """Dream operations are replayed; deleting twice must converge, not raise."""
    await store.delete(scope_of(), ["mem_never_existed"])


async def test_empty_delete_is_a_no_op(store):
    scope = scope_of()
    await store.upsert(scope, [VectorRecord("mem_1", A)])
    await store.delete(scope, [])
    assert await store.count(scope) == 1


async def test_clear_empties_only_that_scope(store):
    mine, theirs = scope_of(), scope_of(client="other")
    await store.upsert(mine, [VectorRecord("mem_1", A)])
    await store.upsert(theirs, [VectorRecord("mem_2", A)])

    await store.clear(mine)

    assert await store.count(mine) == 0
    assert await store.count(theirs) == 1


# --------------------------------------------------------------------------- #
# Pre-filtering
#
# The engine narrows to eligible ids in MongoDB, then asks the index to rank
# only those. Getting this wrong lets a superseded memory outrank a current one.
# --------------------------------------------------------------------------- #


async def test_allowed_ids_restricts_the_result(store):
    scope = scope_of()
    await store.upsert(
        scope, [VectorRecord("mem_1", A), VectorRecord("mem_2", A_NEAR), VectorRecord("mem_3", B)]
    )

    hits = await store.search(scope, A, top_k=5, allowed_ids={"mem_3"})

    assert {hit.memory_id for hit in hits} == {"mem_3"}


async def test_an_empty_allow_list_means_nothing_is_eligible(store):
    """`set()` must not be read as "no filter" — the pre-filter matched nothing,
    and returning everything would defeat it entirely."""
    scope = scope_of()
    await store.upsert(scope, [VectorRecord("mem_1", A)])

    assert await store.search(scope, A, top_k=5, allowed_ids=set()) == []


async def test_none_allow_list_means_no_filter(store):
    scope = scope_of()
    await store.upsert(scope, [VectorRecord("mem_1", A)])

    assert len(await store.search(scope, A, top_k=5, allowed_ids=None)) == 1


async def test_filtering_surfaces_matches_that_rank_below_the_top(store):
    """A pre-filter must reach past the nearest neighbours, not just filter them.

    This is the case a naive "fetch top_k then filter" implementation fails: the
    only eligible row is the least similar one.
    """
    scope = scope_of()
    await store.upsert(
        scope,
        [VectorRecord(f"near_{i}", A_NEAR) for i in range(10)] + [VectorRecord("far", B)],
    )

    hits = await store.search(scope, A, top_k=3, allowed_ids={"far"})

    assert [hit.memory_id for hit in hits] == ["far"]


# --------------------------------------------------------------------------- #
# Tenant isolation
#
# The reason this file exists.
# --------------------------------------------------------------------------- #


async def test_clients_cannot_see_each_others_vectors(store):
    mine, theirs = scope_of(client="acme"), scope_of(client="globex")
    await store.upsert(mine, [VectorRecord("mem_mine", A)])
    await store.upsert(theirs, [VectorRecord("mem_theirs", A)])

    assert [h.memory_id for h in await store.search(mine, A, top_k=10)] == ["mem_mine"]
    assert [h.memory_id for h in await store.search(theirs, A, top_k=10)] == ["mem_theirs"]


async def test_bots_within_one_client_are_isolated(store):
    support, sales = scope_of(bot="support"), scope_of(bot="sales")
    await store.upsert(support, [VectorRecord("mem_support", A)])
    await store.upsert(sales, [VectorRecord("mem_sales", A)])

    assert [h.memory_id for h in await store.search(support, A, top_k=10)] == ["mem_support"]


async def test_users_within_one_bot_are_isolated(store):
    first, second = scope_of(user="u1"), scope_of(user="u2")
    await store.upsert(first, [VectorRecord("mem_first", A)])
    await store.upsert(second, [VectorRecord("mem_second", A)])

    assert [h.memory_id for h in await store.search(first, A, top_k=10)] == ["mem_first"]


async def test_deleting_in_one_scope_leaves_another_untouched(store):
    mine, theirs = scope_of(client="acme"), scope_of(client="globex")
    await store.upsert(mine, [VectorRecord("shared_id", A)])
    await store.upsert(theirs, [VectorRecord("shared_id", A)])

    await store.delete(mine, ["shared_id"])

    assert await store.count(mine) == 0
    assert await store.count(theirs) == 1, "the same id in another tenant must survive"


async def test_the_same_memory_id_in_two_scopes_stays_separate(store):
    """Memory ids are unique per user, not globally. A store keyed only by id
    would silently overwrite one tenant's vector with another's."""
    mine, theirs = scope_of(client="acme"), scope_of(client="globex")
    await store.upsert(mine, [VectorRecord("mem_1", A)])
    await store.upsert(theirs, [VectorRecord("mem_1", B)])

    assert (await store.search(mine, A, top_k=1))[0].score == pytest.approx(1.0, abs=0.01)
    assert (await store.search(theirs, B, top_k=1))[0].score == pytest.approx(1.0, abs=0.01)


async def test_allow_list_cannot_reach_across_scopes(store):
    """Even naming another tenant's id explicitly must return nothing."""
    mine, theirs = scope_of(client="acme"), scope_of(client="globex")
    await store.upsert(theirs, [VectorRecord("mem_theirs", A)])

    assert await store.search(mine, A, top_k=10, allowed_ids={"mem_theirs"}) == []


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


async def test_store_reports_a_name(store):
    assert isinstance(store.name, str) and store.name


async def test_health_check_passes_on_a_working_store(store):
    assert await store.health_check() is True
