"""One SDK surface, four classes.

`MemDream`, `AsyncMemDream`, `DreamClient` and `AsyncDreamClient` promise the
same methods, arguments and return types, so moving from embedded to hosted
changes the constructor and nothing else.

That promise had quietly broken, in ways a customer would hit in their first
five minutes:

* `DreamClient(...).add()` did not exist — only `client.memory.add()` did —
  although the package docstring showed the former;
* the HTTP clients were missing `timestamp`, `as_of`, `include_expired`,
  `valid_from`, `valid_until` and `min_memories`;
* the async client dropped `agent_id` and `namespace` on every call;
* neither SDK could run a consolidation pass;
* a memory written into a `namespace` could never be listed or fetched, because
  read endpoints did not accept one.

The older parity test compared method *names*, in one direction. These compare
full signatures in both, and check that what a signature accepts actually
reaches the wire.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.core.identity import build_scope
from app.main import create_app
from app.models.memory import Memory, utcnow
from memdream import (
    AsyncDreamClient,
    AsyncMemDream,
    ConsolidationReport,
    DreamClient,
    MemDream,
)
from memdream.client import (
    DREAM_OPERATIONS,
    MEMORY_OPERATIONS,
    _AsyncDreamAPI,
    _AsyncMemoryAPI,
    _DreamAPI,
    _MemoryAPI,
)
from memdream.exceptions import DreamAPIError, DreamNotFoundError, DreamValidationError

pytestmark = pytest.mark.asyncio

USER = "parity_user"
OPERATIONS = MEMORY_OPERATIONS + DREAM_OPERATIONS + ("wait_for",)


def _shape(fn) -> list[tuple[str, object]]:
    """Argument names and defaults, ignoring `self` and the `**scope` catch-all."""
    return [
        (name, parameter.default)
        for name, parameter in inspect.signature(fn).parameters.items()
        if name != "self"
        and parameter.kind not in (parameter.VAR_KEYWORD, parameter.VAR_POSITIONAL)
    ]


class _Recorder:
    """An httpx transport that records requests and answers with an empty body
    every parser tolerates — for checking what reaches the wire."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            json={
                "action": "NOOP", "results": [], "memories": [], "count": 0,
                "jobs": [], "operations": [], "jobId": "dream_job_x", "status": "completed",
            },
        )

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def body(self) -> dict:
        return json.loads(self.last.content or b"{}")


def _recording_client(recorder: _Recorder, **kwargs) -> tuple[AsyncDreamClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder), base_url="http://memdream.test")
    return AsyncDreamClient(http_client=http, **kwargs), http


# --------------------------------------------------------------------------- #
# The surface
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cls", [DreamClient, AsyncDreamClient, MemDream, AsyncMemDream])
async def test_every_operation_is_callable_flat(cls):
    missing = [name for name in OPERATIONS if not callable(getattr(cls, name, None))]
    assert not missing, f"{cls.__name__} is missing {missing}"


@pytest.mark.parametrize("name", OPERATIONS)
async def test_hosted_and_embedded_signatures_match(name):
    """Names alone are not parity. `search(as_of=...)` working embedded and
    raising `TypeError` hosted is exactly the break this exists to catch."""
    embedded = _shape(getattr(AsyncMemDream, name))

    hosted = [DreamClient, AsyncDreamClient]
    hosted += [_MemoryAPI, _AsyncMemoryAPI] if name in MEMORY_OPERATIONS else [_DreamAPI, _AsyncDreamAPI]
    for cls in hosted:
        assert _shape(getattr(cls, name)) == embedded, (
            f"{cls.__name__}.{name} differs from AsyncMemDream.{name}"
        )


async def test_the_flat_and_namespaced_forms_are_the_same_call():
    recorder = _Recorder()
    client, http = _recording_client(recorder, user_id=USER)

    await client.search("coffee", limit=3)
    await client.memory.search("coffee", limit=3)

    first, second = recorder.requests
    assert (first.method, first.url, first.content) == (second.method, second.url, second.content)
    await http.aclose()


async def test_the_sync_client_works_flat_as_documented():
    """The headline break: the package docstring showed `mem.add()` on a
    `DreamClient`, and it raised `AttributeError`."""
    recorder = _Recorder()
    http = httpx.Client(transport=httpx.MockTransport(recorder), base_url="http://memdream.test")
    client = DreamClient(http_client=http, user_id=USER)

    assert client.add("I moved to Bangalore.").action == "NOOP"
    assert recorder.last.url.path == "/v1/memories"
    assert client.memory.add("again").action == "NOOP"
    http.close()


# --------------------------------------------------------------------------- #
# What reaches the wire
# --------------------------------------------------------------------------- #


async def test_the_async_client_forwards_agent_and_namespace_per_call():
    """It used to drop both, so a call meant for one partition read another."""
    recorder = _Recorder()
    client, http = _recording_client(recorder, user_id=USER)

    await client.search("x", agent_id="support", namespace={"env": "prod"})

    body = recorder.body()
    assert body["agentId"] == "support"
    assert body["namespace"] == {"env": "prod"}
    await http.aclose()


async def test_every_documented_argument_reaches_the_wire():
    recorder = _Recorder()
    client, http = _recording_client(recorder, user_id=USER)
    when = datetime(2026, 3, 1, tzinfo=timezone.utc)

    await client.add("hi", timestamp=when)
    assert recorder.body()["timestamp"].startswith("2026-03-01")

    await client.search("x", as_of=when, include_expired=True)
    body = recorder.body()
    assert body["asOf"].startswith("2026-03-01")
    assert body["includeExpired"] is True

    await client.create("User likes tea.", valid_from=when, valid_until=when + timedelta(days=1))
    body = recorder.body()
    assert body["validFrom"].startswith("2026-03-01")
    assert body["validUntil"].startswith("2026-03-02")

    await client.synthesize(min_memories=5)
    assert recorder.body()["minMemories"] == 5

    await client.consolidate(
        decay=True, decay_after_days=30, recall_epoch="2026-01-01",
        compact=True, compact_min_memories=10,
    )
    expected = {
        "dryRun": True, "decay": True, "decayAfterDays": 30,
        "recallEpoch": "2026-01-01", "compact": True, "compactMinMemories": 10,
    }
    body = recorder.body()
    assert {key: body.get(key) for key in expected} == expected

    await client.audit(memory_id="mem_1")
    assert recorder.last.url.path == "/v1/memories/mem_1/audit"
    await http.aclose()


async def test_consolidate_is_a_dry_run_unless_you_say_otherwise():
    recorder = _Recorder()
    client, http = _recording_client(recorder, user_id=USER)

    await client.consolidate()
    assert recorder.body()["dryRun"] is True

    await client.consolidate(dry_run=False)
    assert recorder.body()["dryRun"] is False
    await http.aclose()


async def test_read_endpoints_carry_namespace_as_json():
    recorder = _Recorder()
    client, http = _recording_client(recorder, user_id=USER, namespace={"region": "in", "env": "prod"})

    await client.list()

    assert json.loads(recorder.last.url.params["namespace"]) == {"env": "prod", "region": "in"}
    await http.aclose()


async def test_a_misspelt_scope_argument_is_refused_before_sending():
    """Dropped silently, `clinet_id` would read a different partition and come
    back empty — an answer that looks exactly like a real one."""
    recorder = _Recorder()
    client, http = _recording_client(recorder, user_id=USER)

    with pytest.raises(TypeError, match="clinet_id"):
        await client.search("x", clinet_id="acme")

    assert not recorder.requests, "nothing should have been sent"
    await http.aclose()


async def test_the_embedded_sdk_refuses_a_misspelt_scope_argument_too(container):
    mem = AsyncMemDream(container=container, user_id=USER)

    with pytest.raises(TypeError, match="clinet_id"):
        await mem.search("x", clinet_id="acme")


async def test_an_api_key_is_sent_even_with_your_own_http_client():
    """It was silently ignored when `http_client` was supplied, which surfaces
    as a 401 that looks like a bad key."""
    recorder = _Recorder()
    client, http = _recording_client(recorder, user_id=USER, api_key="sk_test")

    await client.stats()

    assert recorder.last.headers["X-API-Key"] == "sk_test"
    await http.aclose()


# --------------------------------------------------------------------------- #
# End to end, through the real app
# --------------------------------------------------------------------------- #


@pytest.fixture
async def hosted(container):
    app = create_app()
    app.state.container = container
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    yield AsyncDreamClient(http_client=http, user_id=USER)
    await http.aclose()


async def _lapsed(container) -> Memory:
    """A memory whose validity window closed yesterday — provably false."""
    scope = build_scope(user_id=USER, settings=container.settings)
    content = "User is on leave until last Friday."
    memory = Memory(
        **scope.as_model_fields(),
        content=content,
        importance=0.3,
        created_at=utcnow() - timedelta(days=40),
        valid_until=utcnow() - timedelta(days=1),
        embedding=await container.embeddings.embed(content),
        embedding_model=container.embeddings.model_name,
    )
    await container.lifecycle.activate(scope, memory)
    return memory


async def test_consolidate_through_the_sdk_reports_without_acting(hosted, container):
    lapsed = await _lapsed(container)

    report = await hosted.consolidate()

    assert isinstance(report, ConsolidationReport)
    assert report.dry_run is True
    assert report.lapsed == 1
    assert report.expired == 0
    assert report.would_retire == 1
    assert (await hosted.get(lapsed.memory_id)).memory.is_active


async def test_applying_through_the_sdk_retires_the_memory(hosted, container):
    lapsed = await _lapsed(container)

    report = await hosted.consolidate(dry_run=False)

    assert report.expired == 1
    assert lapsed.memory_id in report.expired_ids
    expired = await hosted.list(status=["expired"])
    assert lapsed.memory_id in [memory.memory_id for memory in expired]


async def test_hosted_and_embedded_consolidate_agree(hosted, container):
    """One implementation behind both, so the same data gives the same report."""
    await _lapsed(container)
    embedded = AsyncMemDream(container=container, user_id=USER)

    over_http = await hosted.consolidate()
    in_process = await embedded.consolidate()

    fields = ("dry_run", "scanned", "lapsed", "decayed", "expired", "protected", "decay_applied")
    assert {f: getattr(over_http, f) for f in fields} == {f: getattr(in_process, f) for f in fields}


async def test_a_namespaced_memory_can_be_read_back(hosted):
    """Before read endpoints accepted `namespace`, this memory could be written
    and searched but never listed, fetched or erased."""
    created = await hosted.create("User prefers email receipts.", namespace={"env": "prod"})
    memory_id = created.memory.memory_id

    listed = await hosted.list(namespace={"env": "prod"})
    assert memory_id in [memory.memory_id for memory in listed]
    fetched = await hosted.get(memory_id, namespace={"env": "prod"})
    assert fetched.memory.memory_id == memory_id

    # And it stays in its own partition.
    assert memory_id not in [memory.memory_id for memory in await hosted.list()]
    with pytest.raises(DreamNotFoundError):
        await hosted.get(memory_id)


async def test_a_malformed_namespace_is_rejected_not_ignored(container):
    app = create_app()
    app.state.container = container
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        not_json = await http.get("/v1/memories", params={"userId": USER, "namespace": "env=prod"})
        wrong_shape = await http.get("/v1/memories", params={"userId": USER, "namespace": '["prod"]'})

    for response in (not_json, wrong_shape):
        assert response.status_code == 422
        assert "namespace" in response.json()["error"]["message"]


async def test_per_memory_audit_is_scope_checked(hosted, container):
    """The audit trail is keyed by memory id, so reading it without checking the
    memory belongs to the caller would bypass tenancy."""
    created = await hosted.create("User likes filter coffee.")
    memory_id = created.memory.memory_id

    assert await hosted.audit(memory_id=memory_id)

    with pytest.raises(DreamNotFoundError):
        await hosted.audit(memory_id=memory_id, user_id="someone_else")

    intruder = AsyncMemDream(container=container, user_id="someone_else")
    with pytest.raises(DreamNotFoundError):
        await intruder.audit(memory_id=memory_id)


# --------------------------------------------------------------------------- #
# The same failures, the same exceptions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", ["hosted", "embedded"])
async def test_failures_raise_the_same_types_in_both_modes(mode, hosted, container):
    """An `except DreamValidationError:` written against one mode must not
    silently miss in the other. Embedded used to leak the engine's internal
    exceptions: a pydantic `ValidationError`, an `InvalidStateTransitionError`."""
    mem = hosted if mode == "hosted" else AsyncMemDream(container=container, user_id=USER)

    with pytest.raises(DreamValidationError):
        await mem.add("")
    with pytest.raises(DreamValidationError):
        await mem.create("User likes tea.", importance=2.0)
    with pytest.raises(DreamNotFoundError):
        await mem.get("mem_does_not_exist")

    await mem.add("I live in Chennai.")
    moved = await mem.add("I moved to Bangalore.")
    assert moved.superseded_memory_ids, "the offline reasoner should supersede here"

    with pytest.raises(DreamAPIError) as exc:
        await mem.update(moved.superseded_memory_ids[0], content="User lives in Pune.")
    assert exc.value.status_code == 409
    assert exc.value.code == "invalid_state_transition"

    with pytest.raises(ValueError, match="at least one field"):
        await mem.update(moved.memory_id)
    with pytest.raises(ValueError, match="confirm"):
        await mem.purge()


@pytest.mark.parametrize("mode", ["hosted", "embedded"])
async def test_missing_identity_is_a_local_value_error_in_both_modes(mode, container):
    http = None
    if mode == "hosted":
        mem, http = _recording_client(_Recorder())
    else:
        mem = AsyncMemDream(container=container)

    with pytest.raises(ValueError, match="user_id or phone_number"):
        await mem.search("x")

    if http is not None:
        await http.aclose()


async def test_purge_reports_the_same_shape_in_both_modes(hosted, container):
    await hosted.add("I live in Chennai.")
    over_http = await hosted.purge(confirm=True)

    embedded = AsyncMemDream(container=container, user_id=USER)
    await embedded.add("I live in Chennai.")
    in_process = await embedded.purge(confirm=True)

    assert set(over_http) == set(in_process)
    assert in_process["deletedMemories"] >= 1
