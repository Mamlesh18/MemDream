"""HTTP-level tests.

The app is built without its lifespan and the test container is injected
directly, so these exercise routing, validation, serialisation and error
translation without needing MongoDB, Redis or Azure.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app

pytestmark = pytest.mark.asyncio

PHONE = "+919999999999"
CLIENT = "client_test"
BOT = "bot_test"


@pytest.fixture
async def client(container):
    app = create_app()
    # Bypass the lifespan: the fixture container already owns wired fakes.
    app.state.container = container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def scope_params(client_id: str = CLIENT, phone: str = PHONE) -> dict:
    return {"clientId": client_id, "botId": BOT, "phoneNumber": phone}


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


async def test_health_is_liveness_only(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_ready_reports_each_dependency(client):
    response = await client.get("/ready")
    assert response.status_code == 200
    checks = response.json()["checks"]
    assert set(checks) >= {"mongodb", "vector_store", "queue", "embeddings", "llm"}


async def test_metrics_snapshot_is_served(client):
    await client.post(
        "/v1/memories", json={"prompt": "I like black coffee.", **scope_params()}
    )
    response = await client.get("/v1/metrics")
    assert response.status_code == 200
    assert "counters" in response.json()


# --------------------------------------------------------------------------- #
# Add
# --------------------------------------------------------------------------- #


async def test_add_memory_returns_the_spec_shape(client):
    response = await client.post(
        "/v1/memories",
        json={"prompt": "I moved to Bangalore last month.", **scope_params()},
    )
    assert response.status_code == 200

    body = response.json()
    assert body["action"] in ("ADD", "SUPERSEDE", "MERGE", "NOOP")
    assert "memoryId" in body
    assert "supersededMemoryIds" in body
    assert isinstance(body["results"], list)
    assert body["requestId"]


async def test_add_then_supersede_over_http(client):
    first = await client.post(
        "/v1/memories", json={"prompt": "I live in Chennai.", **scope_params()}
    )
    old_id = first.json()["memoryId"]

    second = await client.post(
        "/v1/memories", json={"prompt": "I moved to Bangalore.", **scope_params()}
    )
    body = second.json()
    assert body["action"] == "SUPERSEDE"
    assert old_id in body["supersededMemoryIds"]


async def test_add_requires_an_identity(client):
    response = await client.post(
        "/v1/memories", json={"prompt": "hello", "clientId": CLIENT, "botId": BOT}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_unknown_fields_are_rejected(client):
    response = await client.post(
        "/v1/memories",
        json={"prompt": "hello", "unexpected": "field", **scope_params()},
    )
    assert response.status_code == 422


async def test_response_carries_a_request_id_header(client):
    response = await client.get("/health")
    assert response.headers.get("X-Request-ID")


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


async def test_search_returns_scored_memories(client):
    await client.post(
        "/v1/memories", json={"prompt": "I live in Bangalore.", **scope_params()}
    )
    response = await client.post(
        "/v1/memories/search",
        json={"query": "Where does the user live?", "limit": 5, **scope_params()},
    )
    assert response.status_code == 200

    body = response.json()
    assert body["count"] >= 1
    first = body["memories"][0]
    assert "score" in first
    # Ranking must be explainable, not just a number.
    assert set(first["scoreBreakdown"]) == {
        "semanticSimilarity",
        "importance",
        "confidence",
        "recency",
        "status",
    }
    # Embeddings never travel to API clients.
    assert "embedding" not in first


async def test_search_latest_only_excludes_history(client):
    await client.post("/v1/memories", json={"prompt": "I live in Chennai.", **scope_params()})
    await client.post("/v1/memories", json={"prompt": "I moved to Bangalore.", **scope_params()})

    response = await client.post(
        "/v1/memories/search",
        json={
            "query": "Where does the user live?",
            "latestOnly": True,
            **scope_params(),
        },
    )
    statuses = {m["status"] for m in response.json()["memories"]}
    assert statuses == {"active"}


# --------------------------------------------------------------------------- #
# Get / relationships
# --------------------------------------------------------------------------- #


async def test_get_memory_exposes_relationships(client):
    await client.post("/v1/memories", json={"prompt": "I live in Chennai.", **scope_params()})
    second = await client.post(
        "/v1/memories", json={"prompt": "I moved to Bangalore.", **scope_params()}
    )
    memory_id = second.json()["memoryId"]

    response = await client.get(f"/v1/memories/{memory_id}", params=scope_params())
    assert response.status_code == 200

    body = response.json()
    assert body["memory"]["memoryId"] == memory_id
    assert body["relationships"], "supersede should have produced an edge"


async def test_get_memory_from_another_client_is_not_found(client):
    created = await client.post(
        "/v1/memories", json={"prompt": "I live in Chennai.", **scope_params()}
    )
    memory_id = created.json()["memoryId"]

    # Same memory id, different tenant.
    response = await client.get(
        f"/v1/memories/{memory_id}", params=scope_params(client_id="client_intruder")
    )
    assert response.status_code == 404


async def test_memory_history_walks_the_supersede_chain(client):
    await client.post("/v1/memories", json={"prompt": "I live in Chennai.", **scope_params()})
    second = await client.post(
        "/v1/memories", json={"prompt": "I moved to Bangalore.", **scope_params()}
    )

    response = await client.get(
        f"/v1/memories/{second.json()['memoryId']}/history", params=scope_params()
    )
    assert response.status_code == 200
    assert response.json()["count"] == 2


async def test_audit_log_records_every_decision(client):
    await client.post("/v1/memories", json={"prompt": "I live in Chennai.", **scope_params()})
    await client.post("/v1/memories", json={"prompt": "I moved to Bangalore.", **scope_params()})

    response = await client.get("/v1/memories/audit/operations", params=scope_params())
    assert response.status_code == 200

    operations = response.json()["operations"]
    assert len(operations) >= 2
    assert {op["action"] for op in operations} & {"ADD", "SUPERSEDE"}
    for op in operations:
        assert op["reason"], "every Dream operation must record why"


# --------------------------------------------------------------------------- #
# Dream
# --------------------------------------------------------------------------- #


async def test_synthesize_queues_a_job(client):
    response = await client.post("/v1/dream/synthesize", json=scope_params())
    assert response.status_code == 200

    body = response.json()
    assert body["jobId"].startswith("dream_job_")
    assert body["status"] in ("queued", "running", "completed", "skipped")


async def test_dream_job_status_is_scoped(client, container):
    created = await client.post("/v1/dream/synthesize", json={"wait": True, **scope_params()})
    job_id = created.json()["jobId"]

    ok = await client.get(f"/v1/dream/jobs/{job_id}", params=scope_params())
    assert ok.status_code == 200
    assert ok.json()["jobId"] == job_id

    # A job id is a handle, not an authorisation token.
    denied = await client.get(
        f"/v1/dream/jobs/{job_id}", params=scope_params(client_id="client_intruder")
    )
    assert denied.status_code == 404


async def test_dream_stats_reports_counts_and_actions(client):
    await client.post("/v1/memories", json={"prompt": "I live in Chennai.", **scope_params()})

    response = await client.get("/v1/dream/stats", params=scope_params())
    assert response.status_code == 200

    body = response.json()
    assert body["counts"]["active"] >= 1
    assert body["actions"]
