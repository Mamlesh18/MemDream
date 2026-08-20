"""API-key binding: the tenant comes from the credential, not the request.

Until this existed, `clientId` arrived in the request body. Self-hosted that is
correct — the caller owns the installation. Hosted it is a critical hole: any
valid key could name any client and read another customer's memories.

The tests below are the proof that the hole is closed, and that closing it did
not break the self-hosted case.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.auth import ApiKeyRegistry, Principal, hash_key
from app.core.config import Settings
from app.core.exceptions import UnauthorizedError
from app.main import create_app

pytestmark = pytest.mark.asyncio


def _settings(**overrides) -> Settings:
    return Settings(env="test", identity_salt="t", **overrides)


# --------------------------------------------------------------------------- #
# The three modes
# --------------------------------------------------------------------------- #


async def test_no_keys_configured_means_no_authentication():
    """Correct behind a private gateway, and why auth is opt-in."""
    registry = ApiKeyRegistry(_settings())

    principal = await registry.resolve(None)

    assert principal.authenticated is False
    assert principal.bound is False
    # The caller owns the deployment, so it may name any tenant.
    assert principal.resolve_client_id("anything") == "anything"


async def test_shared_keys_authenticate_but_do_not_bind():
    registry = ApiKeyRegistry(_settings(api_keys="secret1,secret2"))

    principal = await registry.resolve("secret1")

    assert principal.authenticated is True
    assert principal.bound is False
    # Single-tenant: the body still decides.
    assert principal.resolve_client_id("acme") == "acme"


async def test_bound_keys_carry_their_tenant():
    registry = ApiKeyRegistry(_settings(api_key_clients="k-acme:acme,k-globex:globex"))

    acme = await registry.resolve("k-acme")
    globex = await registry.resolve("k-globex")

    assert acme.bound is True
    assert acme.client_id == "acme"
    assert globex.client_id == "globex"


# --------------------------------------------------------------------------- #
# The security property
# --------------------------------------------------------------------------- #


async def test_a_bound_key_cannot_name_another_tenant():
    """The hole this whole module exists to close."""
    registry = ApiKeyRegistry(_settings(api_key_clients="k-acme:acme"))
    principal = await registry.resolve("k-acme")

    with pytest.raises(UnauthorizedError, match="bound to a different client"):
        principal.resolve_client_id("globex")


async def test_a_conflicting_tenant_is_refused_not_silently_overridden():
    """Refusing surfaces both an integration bug and a deliberate probe.
    Silently overriding would hide each of them."""
    principal = Principal(client_id="acme", key_id="k", bound=True, authenticated=True)

    with pytest.raises(UnauthorizedError):
        principal.resolve_client_id("globex")


async def test_omitting_the_tenant_uses_the_key_binding():
    principal = Principal(client_id="acme", key_id="k", bound=True, authenticated=True)
    assert principal.resolve_client_id(None) == "acme"


async def test_naming_your_own_tenant_is_allowed():
    """Sending the right clientId is redundant but not an error — plenty of
    clients will send it out of habit."""
    principal = Principal(client_id="acme", key_id="k", bound=True, authenticated=True)
    assert principal.resolve_client_id("acme") == "acme"


async def test_an_invalid_key_is_rejected():
    registry = ApiKeyRegistry(_settings(api_key_clients="k-acme:acme"))

    with pytest.raises(UnauthorizedError, match="Invalid API key"):
        await registry.resolve("not-a-real-key")


async def test_a_missing_key_is_rejected_when_auth_is_on():
    registry = ApiKeyRegistry(_settings(api_keys="secret"))

    with pytest.raises(UnauthorizedError, match="Missing"):
        await registry.resolve(None)


async def test_unbound_keys_can_be_refused_entirely():
    """So a leftover shared secret cannot be used to reach another tenant."""
    registry = ApiKeyRegistry(
        _settings(api_keys="legacy", api_key_clients="k-acme:acme", api_key_require_binding=True)
    )

    assert (await registry.resolve("k-acme")).client_id == "acme"
    with pytest.raises(UnauthorizedError, match="not bound"):
        await registry.resolve("legacy")


# --------------------------------------------------------------------------- #
# Storage and hygiene
# --------------------------------------------------------------------------- #


async def test_keys_are_compared_as_digests():
    """Nothing should be able to read a key back out of configuration."""
    digest = hash_key("secret")
    assert digest != "secret"
    assert len(digest) == 64
    assert hash_key("secret") == digest


async def test_malformed_bindings_are_skipped_not_crashed():
    """A typo in one binding must not take down the whole deployment."""
    registry = ApiKeyRegistry(_settings(api_key_clients="k-acme:acme,malformed-no-colon"))

    assert (await registry.resolve("k-acme")).client_id == "acme"
    with pytest.raises(UnauthorizedError):
        await registry.resolve("malformed-no-colon")


async def test_resolutions_are_cached_then_invalidated():
    registry = ApiKeyRegistry(_settings(api_key_clients="k-acme:acme"))

    first = await registry.resolve("k-acme")
    second = await registry.resolve("k-acme")
    assert first is second, "a repeat lookup should hit the cache"

    registry.invalidate("k-acme")
    third = await registry.resolve("k-acme")
    assert third is not first
    assert third.client_id == "acme"


# --------------------------------------------------------------------------- #
# End to end over HTTP
# --------------------------------------------------------------------------- #


@pytest.fixture
async def bound_client(container):
    """An app where k-acme is bound to acme and k-globex to globex."""
    container.settings.api_key_clients = ["k-acme:acme", "k-globex:globex"]
    container.settings.api_keys = []
    container.api_keys = ApiKeyRegistry(container.settings, container.database)

    app = create_app()
    app.state.container = container
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def _body(**extra) -> dict:
    return {"userId": "alice", **extra}


async def test_request_without_a_key_is_rejected(bound_client):
    response = await bound_client.post(
        "/v1/memories", json={"prompt": "hello", **_body()}
    )
    assert response.status_code == 401


async def test_bound_key_works_without_naming_a_tenant(bound_client):
    response = await bound_client.post(
        "/v1/memories",
        json={"prompt": "I live in Chennai.", **_body()},
        headers={"X-API-Key": "k-acme"},
    )
    assert response.status_code == 200


async def test_bound_key_cannot_reach_another_tenant_over_http(bound_client):
    """The end-to-end version of the attack."""
    response = await bound_client.post(
        "/v1/memories",
        json={"prompt": "I live in Chennai.", **_body(clientId="globex")},
        headers={"X-API-Key": "k-acme"},
    )

    assert response.status_code == 401
    assert "bound to a different client" in response.json()["error"]["message"]


async def test_two_keys_cannot_see_each_others_memories(bound_client, container):
    """Same userId, two tenants. The key is what separates them."""
    await bound_client.post(
        "/v1/memories",
        json={"prompt": "I live in Chennai.", **_body()},
        headers={"X-API-Key": "k-acme"},
    )
    await bound_client.post(
        "/v1/memories",
        json={"prompt": "I live in Mumbai.", **_body()},
        headers={"X-API-Key": "k-globex"},
    )

    acme = await bound_client.post(
        "/v1/memories/search",
        json={"query": "where do they live", **_body()},
        headers={"X-API-Key": "k-acme"},
    )
    contents = [m["content"] for m in acme.json()["memories"]]

    assert any("Chennai" in c for c in contents)
    assert not any("Mumbai" in c for c in contents)


async def test_reads_are_bound_too(bound_client):
    """GET endpoints authenticate through their scope dependency."""
    created = await bound_client.post(
        "/v1/memories",
        json={"prompt": "I live in Chennai.", **_body()},
        headers={"X-API-Key": "k-acme"},
    )
    memory_id = created.json()["memoryId"]

    stolen = await bound_client.get(
        f"/v1/memories/{memory_id}",
        params={"userId": "alice"},
        headers={"X-API-Key": "k-globex"},
    )
    # Another tenant's key resolves to another partition, so the id is not found.
    assert stolen.status_code == 404
