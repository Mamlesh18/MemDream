"""`POST /v1/dream/consolidate` — the nightly pass, on demand.

This is the only endpoint in MemDream that retires memories without a user
asking it to, so the tests here are less about the happy path than about the
three ways an endpoint like this goes wrong:

    it acts when you only asked it to look     -> dryRun defaults to true
    it changes the rules for everyone else     -> overrides are per-call copies
    it claims work it declined to do           -> expired counts actions, not
                                                  candidates

The consolidation logic itself is covered by `test_consolidation.py` and
`test_decay_and_schedule.py`; this file is the HTTP seam over it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.models.enums import MemoryStatus, TemporalNature
from app.models.memory import Memory, utcnow

pytestmark = pytest.mark.asyncio

PHONE = "+919999999999"
CLIENT = "client_test"
BOT = "bot_test"

PATH = "/v1/dream/consolidate"


@pytest.fixture
async def client(container):
    app = create_app()
    app.state.container = container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def body(**overrides) -> dict:
    return {"clientId": CLIENT, "botId": BOT, "phoneNumber": PHONE, **overrides}


async def _store(container, scope, content, **overrides):
    """A memory with exact control over its dates, which a conversation cannot give."""
    overrides.setdefault("importance", 0.2)
    overrides.setdefault("created_at", utcnow() - timedelta(days=200))
    memory = Memory(
        **scope.as_model_fields(),
        content=content,
        embedding=await container.embeddings.embed(content),
        embedding_model=container.embeddings.model_name,
        **overrides,
    )
    await container.lifecycle.activate(scope, memory)
    return memory


@pytest.fixture
async def lapsed(container, scope):
    """A memory whose validity window closed yesterday — provably false."""
    return await _store(
        container, scope, "User is on leave until the end of last month.",
        valid_until=utcnow() - timedelta(days=1),
    )


# --------------------------------------------------------------------------- #
# The default is to look, not to act
# --------------------------------------------------------------------------- #


async def test_it_reports_without_changing_anything_by_default(client, container, scope, lapsed):
    """`dryRun` is absent from the body, so it must default to true.

    An endpoint that retires memories on a missing field is one nobody should
    have to remember to disarm.
    """
    response = await client.post(PATH, json=body())

    assert response.status_code == 200
    payload = response.json()
    assert payload["dryRun"] is True
    assert payload["lapsed"] == 1, "it should still say what it found"
    assert payload["expired"] == 0, "but it must not have acted"

    stored = await container.memories.get(scope, lapsed.memory_id)
    assert stored.status is MemoryStatus.ACTIVE


async def test_applying_requires_saying_so(client, container, scope, lapsed):
    response = await client.post(PATH, json=body(dryRun=False))

    assert response.status_code == 200
    payload = response.json()
    assert payload["expired"] == 1
    assert lapsed.memory_id in payload["expiredIds"]

    stored = await container.memories.get(scope, lapsed.memory_id)
    assert stored.status is MemoryStatus.EXPIRED


async def test_a_retired_memory_is_kept_not_deleted(client, container, scope, lapsed):
    """The rule the whole design rests on. Forgetting is a status transition, so
    being wrong costs a flip rather than a row."""
    await client.post(PATH, json=body(dryRun=False))

    stored = await container.memories.get(scope, lapsed.memory_id)
    assert stored is not None
    assert stored.content == lapsed.content


# --------------------------------------------------------------------------- #
# Overrides are per-call
# --------------------------------------------------------------------------- #


async def test_overrides_do_not_leak_into_configuration(client, container):
    """The failure this endpoint would be remembered for.

    One process serves every tenant. If a request could flip `decayEnabled` on
    the live settings object, one caller's experiment would quietly change what
    tonight's pass does to everybody else.
    """
    before = container.settings.dream.consolidation.model_copy(deep=True)

    await client.post(
        PATH,
        json=body(decay=True, decayAfterDays=1, decayImportanceFloor=0.99,
                  recallEpoch="2020-01-01"),
    )

    assert container.settings.dream.consolidation == before


async def test_decay_can_be_applied_for_one_call(client, container, scope):
    """Old, low importance, never recalled — and an epoch, so the rule can fire."""
    forgotten = await _store(container, scope, "User mentioned enjoying jazz on the radio.")

    response = await client.post(
        PATH,
        json=body(dryRun=False, decay=True, decayAfterDays=30, recallEpoch="2020-01-01"),
    )

    payload = response.json()
    assert payload["decayApplied"] is True
    assert forgotten.memory_id in payload["expiredIds"]


async def test_decay_without_an_epoch_does_nothing(client, container, scope):
    """The cold-start guard, reachable over HTTP.

    Every memory written before recall tracking existed reads
    `lastRecalledAt: null`, which is indistinguishable from "nothing ever wanted
    this" — so without an epoch, day one would expire the whole database.
    """
    forgotten = await _store(container, scope, "User mentioned enjoying jazz on the radio.")

    response = await client.post(
        PATH, json=body(dryRun=False, decay=True, decayAfterDays=1, recallEpoch=""),
    )

    payload = response.json()
    assert payload["decayApplied"] is False
    assert payload["expired"] == 0
    assert (
        await container.memories.get(scope, forgotten.memory_id)
    ).status is MemoryStatus.ACTIVE


async def test_candidates_are_reported_while_decay_is_off(client, container, scope):
    """Measurement mode: this is how a threshold is chosen before anything acts
    on it, and it must not be confused with work done."""
    await _store(container, scope, "User mentioned enjoying jazz on the radio.")

    response = await client.post(
        PATH, json=body(dryRun=False, decayAfterDays=30, recallEpoch="2020-01-01"),
    )

    payload = response.json()
    assert payload["decayed"] == 1, "the candidate should be reported"
    assert payload["expired"] == 0, "but decay was never asked for"
    assert payload["decayApplied"] is False


# --------------------------------------------------------------------------- #
# Guard rails, over HTTP
# --------------------------------------------------------------------------- #


async def test_importance_outranks_disuse(client, container, scope):
    """A peanut allergy nobody has asked about in a year still outranks
    yesterday's lunch order."""
    allergy = await _store(
        container, scope, "User is allergic to peanuts.", importance=0.95,
    )

    await client.post(
        PATH,
        json=body(dryRun=False, decay=True, decayAfterDays=1, recallEpoch="2020-01-01"),
    )

    assert (
        await container.memories.get(scope, allergy.memory_id)
    ).status is MemoryStatus.ACTIVE


async def test_permanent_facts_are_never_decayed(client, container, scope):
    born = await _store(
        container, scope, "User was born in Chennai.",
        temporal_nature=TemporalNature.PERMANENT,
    )

    await client.post(
        PATH,
        json=body(dryRun=False, decay=True, decayAfterDays=1, recallEpoch="2020-01-01"),
    )

    assert (
        await container.memories.get(scope, born.memory_id)
    ).status is MemoryStatus.ACTIVE


# --------------------------------------------------------------------------- #
# Compaction, and the cost gate
# --------------------------------------------------------------------------- #


async def test_compaction_is_not_run_unless_asked(client, container, scope, llm):
    """It is the only part of a pass that costs a model call, so it is opt-in
    per request as well as in configuration."""
    await _store(container, scope, "User ordered a masala dosa.")
    before = len(llm.calls)

    response = await client.post(PATH, json=body())

    assert response.json()["clusters"] == 0
    assert len(llm.calls) == before


async def test_compaction_reports_what_it_folded(client, container, scope):
    config = container.settings.dream.compaction
    config.min_cluster_size = 3
    config.similarity_threshold = 0.15  # the offline embeddings score low

    for index in range(6):
        await _store(container, scope, f"User ordered a masala dosa on day {index}.")

    response = await client.post(
        PATH, json=body(dryRun=False, compact=True, compactMinMemories=4)
    )

    payload = response.json()
    assert payload["clusters"] >= 1
    assert payload["absorbed"] >= 3
    assert payload["compacted"] >= 1


async def test_the_cost_gate_can_be_lowered_for_one_call(client, container, scope):
    """`min_memories` defaults high so a nightly pass over a thousand users stays
    affordable — which means a small account sees compaction do nothing, and the
    obvious conclusion is that it is broken. Lowering it per call is how you see
    the machinery work without shipping it lowered."""
    before = container.settings.dream.compaction.min_memories
    container.settings.dream.compaction.min_cluster_size = 3
    container.settings.dream.compaction.similarity_threshold = 0.15

    for index in range(6):
        await _store(container, scope, f"User ordered a masala dosa on day {index}.")

    gated = await client.post(PATH, json=body(compact=True))
    assert gated.json()["clusters"] == 0, "the default gate should hold"

    lowered = await client.post(PATH, json=body(compact=True, compactMinMemories=4))
    assert lowered.json()["clusters"] >= 1

    assert container.settings.dream.compaction.min_memories == before, (
        "the override must not have changed configuration"
    )


# --------------------------------------------------------------------------- #
# The seam itself
# --------------------------------------------------------------------------- #


async def test_identity_is_required(client):
    """`_ScopedRequest` insists on a user, so a body without one is a 422 rather
    than a pass over an empty scope."""
    response = await client.post(PATH, json={"clientId": CLIENT})
    assert response.status_code == 422


async def test_another_tenant_sees_none_of_it(client, container, other_scope):
    """Same phone number, different client. The scope key differs, so the pass
    finds nothing — tenancy holds here as everywhere else."""
    await _store(container, other_scope, "User is on leave.",
                 valid_until=utcnow() - timedelta(days=1))

    response = await client.post(PATH, json=body(dryRun=False))

    assert response.json()["scanned"] == 0
    assert response.json()["expired"] == 0


async def test_an_empty_account_is_a_clean_no_op(client):
    response = await client.post(PATH, json=body(dryRun=False))

    payload = response.json()
    assert response.status_code == 200
    assert payload["scanned"] == 0
    assert payload["expired"] == 0
    assert payload["durationMs"] >= 0
