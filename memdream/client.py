"""HTTP clients for a running MemDream server.

    from memdream import DreamClient

    mem = DreamClient("https://memory.example.com", api_key="...", user_id="alice")
    mem.add("I moved to Bangalore last month.")
    print(mem.search("where does the user live?").as_context())

`DreamClient` and `AsyncDreamClient` expose the same methods as `MemDream` and
`AsyncMemDream` — same names, same arguments, same return types — so moving from
embedded to hosted changes the constructor and nothing else.

Every operation is reachable two ways, and both are the same call:

    mem.add(...)            flat, exactly like the embedded SDK
    mem.memory.add(...)     namespaced: `memory.*` and `dream.*`

## One definition, two transports

Each operation is described once, in `_Operations`, as a `_Call`: method, path,
query or body, and how to parse the response. The sync and async clients differ
only in how they send it.

That is deliberate. The two clients used to be written out by hand, twice, and
had drifted — the async one silently dropped `agent_id` and `namespace` on every
call, so a request meant for one partition read from another. With one
definition that class of bug has nowhere to live.

## Scope

Set scope once on the constructor and omit it from every call, or pass it per
call to override: `user_id`, `phone_number`, `agent_id`, `client_id`,
`namespace`, and `bot_id` (the previous name for `agent_id`).

An unrecognised keyword raises `TypeError` instead of being ignored. A misspelt
`clinet_id` that was quietly dropped would read from a different partition than
the one the caller named, and return an empty result that looks like a real one.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from memdream.exceptions import DreamConnectionError, error_for
from memdream.models import (
    AddResult,
    ConsolidationReport,
    DreamJob,
    MemoryDetail,
    MemoryRecord,
    SearchResult,
)

DEFAULT_TIMEOUT = 60.0

#: Keywords every operation accepts for scope.
SCOPE_KEYS = ("user_id", "phone_number", "agent_id", "client_id", "namespace", "bot_id")

#: The shared surface. `MemDream` and `AsyncMemDream` implement exactly these
#: too; `tests/test_sdk_parity.py` holds all four classes to it, signatures
#: included.
MEMORY_OPERATIONS = (
    "add", "create", "update", "forget", "purge",
    "search", "get", "list", "history", "audit",
)
DREAM_OPERATIONS = ("synthesize", "get_job", "list_jobs", "stats", "consolidate")
#: Hosted only — they describe the server, and embedded there is no server.
SERVER_OPERATIONS = ("health", "ready", "metrics", "token_usage")


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #


class _ScopeDefaults:
    """Scope values supplied once and merged into every call.

    Only `user_id` (or `phone_number`) is required. `agent_id`, `client_id` and
    `namespace` are optional partition dimensions — supply the ones your
    deployment has.
    """

    def __init__(
        self,
        user_id: str | None = None,
        phone_number: str | None = None,
        agent_id: str | None = None,
        client_id: str | None = None,
        namespace: dict[str, str] | None = None,
        bot_id: str | None = None,
    ) -> None:
        self.user_id = user_id
        self.phone_number = phone_number
        # `bot_id` is the previous name for `agent_id`.
        self.agent_id = agent_id or bot_id
        self.client_id = client_id
        self.namespace = namespace or {}

    def resolve(
        self,
        client_id: str | None = None,
        bot_id: str | None = None,
        phone_number: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        namespace: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        user = user_id or self.user_id
        phone = phone_number or self.phone_number
        if not user and not phone:
            raise ValueError(
                "Either user_id or phone_number must be supplied, on the call or "
                "as a client default"
            )

        resolved: dict[str, Any] = {}
        if user:
            resolved["userId"] = user
        if phone:
            resolved["phoneNumber"] = phone

        agent = agent_id or bot_id or self.agent_id
        if agent:
            resolved["agentId"] = agent
        tenant = client_id or self.client_id
        if tenant:
            resolved["clientId"] = tenant
        combined = {**self.namespace, **(namespace or {})}
        if combined:
            resolved["namespace"] = combined
        return resolved

    def from_kwargs(self, scope: dict[str, Any], *, query: bool = False) -> dict[str, Any]:
        """Resolve per-call scope keywords, refusing any that are not scope."""
        unknown = set(scope) - set(SCOPE_KEYS)
        if unknown:
            raise TypeError(
                f"Unexpected argument(s): {', '.join(sorted(unknown))}. "
                f"Scope arguments are: {', '.join(SCOPE_KEYS)}"
            )
        resolved = self.resolve(
            scope.get("client_id"),
            scope.get("bot_id"),
            scope.get("phone_number"),
            scope.get("user_id"),
            scope.get("agent_id"),
            scope.get("namespace"),
        )
        if query and "namespace" in resolved:
            # A query string carries flat values and a partition map is not
            # flat, so it travels as JSON. Sorted, so equal maps encode equally.
            resolved["namespace"] = json.dumps(
                resolved["namespace"], sort_keys=True, separators=(",", ":")
            )
        return resolved


# --------------------------------------------------------------------------- #
# Operations, described once
# --------------------------------------------------------------------------- #


@dataclass
class _Call:
    method: str
    path: str
    parse: Callable[[dict[str, Any]], Any]
    params: dict[str, Any] | None = None
    json: dict[str, Any] | None = None


def _iso(value: Any) -> Any:
    """Datetimes and dates as ISO 8601; strings pass through untouched."""
    return value.isoformat() if hasattr(value, "isoformat") else value


def _identity(data: dict[str, Any]) -> dict[str, Any]:
    return data


def _records(data: dict[str, Any]) -> list[MemoryRecord]:
    return [MemoryRecord.from_dict(item) for item in data.get("memories", [])]


def _jobs(data: dict[str, Any]) -> list[DreamJob]:
    return [DreamJob.from_dict(item) for item in data.get("jobs", [])]


def _operations(data: dict[str, Any]) -> list[dict[str, Any]]:
    return data.get("operations", [])


class _Operations:
    """Every operation, described once.

    Each method validates its arguments and returns a `_Call`; none of them
    sends anything. That is what lets one definition serve both transports, and
    why a bad argument fails before a request leaves the process.
    """

    _UPDATABLE = {
        "content": "content",
        "memory_type": "memoryType",
        "importance": "importance",
        "confidence": "confidence",
        "explicit": "explicit",
        "temporal_nature": "temporalNature",
        "valid_from": "validFrom",
        "valid_until": "validUntil",
        "metadata": "metadata",
    }

    def __init__(self, defaults: _ScopeDefaults) -> None:
        self._defaults = defaults

    def _body(self, scope: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
        body = {key: value for key, value in fields.items() if value is not None}
        body.update(self._defaults.from_kwargs(scope))
        return body

    def _query(
        self, scope: dict[str, Any], fields: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params = {key: value for key, value in (fields or {}).items() if value is not None}
        params.update(self._defaults.from_kwargs(scope, query=True))
        return params

    # --- remember ------------------------------------------------------------- #

    def add(
        self,
        prompt: str,
        *,
        message_id: str | None = None,
        timestamp: Any = None,
        metadata: dict[str, Any] | None = None,
        raw: bool = False,
        **scope: Any,
    ) -> _Call:
        """Extract memories from a message and let Dream decide what to do.

        One message can hold several facts, so `results` holds every decision;
        `action` and `memory_id` mirror the most significant one. `raw=True`
        stores the text verbatim as one memory, skipping extraction but keeping
        the ADD/SUPERSEDE/MERGE decision.
        """
        return _Call(
            "POST",
            "/v1/memories",
            AddResult.from_dict,
            json=self._body(
                scope,
                {
                    "prompt": prompt,
                    "messageId": message_id,
                    "timestamp": _iso(timestamp),
                    "metadata": metadata or None,
                    "raw": True if raw else None,
                },
            ),
        )

    def create(
        self,
        content: str,
        *,
        memory_type: str = "other",
        importance: float = 0.6,
        confidence: float = 1.0,
        explicit: bool = True,
        temporal_nature: str = "current",
        valid_from: Any = None,
        valid_until: Any = None,
        metadata: dict[str, Any] | None = None,
        allow_duplicate: bool = False,
        **scope: Any,
    ) -> _Call:
        """Write a memory verbatim, bypassing extraction and Dream reasoning.

        Write content in the third person ("User is allergic to peanuts."),
        matching what extraction produces, so retrieval treats it the same way.
        """
        return _Call(
            "POST",
            "/v1/memories/manual",
            MemoryDetail.from_dict,
            json=self._body(
                scope,
                {
                    "content": content,
                    "memoryType": memory_type,
                    "importance": importance,
                    "confidence": confidence,
                    "explicit": explicit,
                    "temporalNature": temporal_nature,
                    "validFrom": _iso(valid_from),
                    "validUntil": _iso(valid_until),
                    "metadata": metadata or None,
                    "allowDuplicate": allow_duplicate,
                },
            ),
        )

    def update(self, memory_id: str, **changes: Any) -> _Call:
        """Correct an active memory in place. Only the fields you pass change.

        The memory keeps its id. Superseded and merged memories are history and
        cannot be edited — that raises `DreamAPIError` with status 409.
        """
        scope = {key: changes.pop(key) for key in SCOPE_KEYS if key in changes}
        unknown = set(changes) - set(self._UPDATABLE)
        if unknown:
            raise TypeError(
                f"Cannot update {', '.join(sorted(unknown))}. "
                f"Updatable fields are: {', '.join(self._UPDATABLE)}"
            )
        if not changes:
            raise ValueError("Supply at least one field to update")

        body = {self._UPDATABLE[name]: _iso(value) for name, value in changes.items()}
        body.update(self._defaults.from_kwargs(scope))
        return _Call("PATCH", f"/v1/memories/{memory_id}", MemoryDetail.from_dict, json=body)

    def forget(self, memory_id: str, *, permanent: bool = False, **scope: Any) -> _Call:
        """Retire a memory, or erase it.

        The default is reversible: the memory moves to `expired` and leaves the
        search index, so it stops influencing answers but stays auditable.
        `permanent=True` erases the row and cannot be undone.
        """
        return _Call(
            "DELETE",
            f"/v1/memories/{memory_id}",
            _identity,
            params=self._query(scope, {"permanent": "true" if permanent else "false"}),
        )

    def purge(self, *, confirm: bool = False, **scope: Any) -> _Call:
        """Erase everything for one user, audit trail included. Irreversible.

        Requires `confirm=True`, so it cannot happen by typo or by an argument
        arriving as None.
        """
        if confirm is not True:
            raise ValueError(
                "purge() erases all of this user's memory and cannot be undone. "
                "Pass confirm=True if that is what you intend."
            )
        return _Call(
            "DELETE", "/v1/memories", _identity, params=self._query(scope, {"confirm": "ERASE"})
        )

    # --- recall ---------------------------------------------------------------- #

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        latest_only: bool = False,
        include_superseded: bool = True,
        include_merged: bool = False,
        include_expired: bool = False,
        memory_types: list[str] | None = None,
        min_score: float | None = None,
        as_of: Any = None,
        **scope: Any,
    ) -> _Call:
        """Hybrid-ranked search: meaning, importance, confidence, recency and
        status — not similarity alone.

        `latest_only=True` answers "what is true now?". `as_of` answers "what was
        true then?".
        """
        return _Call(
            "POST",
            "/v1/memories/search",
            SearchResult.from_dict,
            json=self._body(
                scope,
                {
                    "query": query,
                    "limit": limit,
                    "latestOnly": latest_only,
                    "includeSuperseded": include_superseded,
                    "includeMerged": include_merged,
                    "includeExpired": include_expired,
                    "memoryTypes": memory_types or None,
                    "minScore": min_score,
                    "asOf": _iso(as_of),
                },
            ),
        )

    def get(self, memory_id: str, **scope: Any) -> _Call:
        """One memory with its relationships, sources and what derives from it."""
        return _Call(
            "GET", f"/v1/memories/{memory_id}", MemoryDetail.from_dict, params=self._query(scope)
        )

    def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: list[str] | None = None,
        memory_type: list[str] | None = None,
        **scope: Any,
    ) -> _Call:
        """Browse without a semantic query. Active memories only by default."""
        return _Call(
            "GET",
            "/v1/memories",
            _records,
            params=self._query(
                scope,
                {
                    "limit": limit,
                    "offset": offset,
                    "status": status or None,
                    "memoryType": memory_type or None,
                },
            ),
        )

    def history(self, memory_id: str, **scope: Any) -> _Call:
        """Follow the supersede chain back to the original fact."""
        return _Call(
            "GET", f"/v1/memories/{memory_id}/history", _records, params=self._query(scope)
        )

    def audit(
        self,
        *,
        memory_id: str | None = None,
        action: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
        **scope: Any,
    ) -> _Call:
        """Every Dream decision for this user, NOOPs included — or, with
        `memory_id`, every decision that touched that one memory.

        `action` and `offset` apply to the user-wide log only.
        """
        if memory_id is not None:
            return _Call(
                "GET",
                f"/v1/memories/{memory_id}/audit",
                _operations,
                params=self._query(scope, {"limit": limit}),
            )
        return _Call(
            "GET",
            "/v1/memories/audit/operations",
            _operations,
            params=self._query(scope, {"action": action or None, "limit": limit, "offset": offset}),
        )

    # --- dream ----------------------------------------------------------------- #

    def synthesize(
        self,
        *,
        wait: bool = False,
        force: bool = False,
        min_memories: int | None = None,
        **scope: Any,
    ) -> _Call:
        """Look for patterns across this user's memories.

        Returns a job immediately; `wait=True` runs it before responding.
        """
        return _Call(
            "POST",
            "/v1/dream/synthesize",
            DreamJob.from_dict,
            json=self._body(scope, {"wait": wait, "force": force, "minMemories": min_memories}),
        )

    def get_job(self, job_id: str, **scope: Any) -> _Call:
        return _Call(
            "GET", f"/v1/dream/jobs/{job_id}", DreamJob.from_dict, params=self._query(scope)
        )

    def list_jobs(self, *, limit: int = 20, offset: int = 0, **scope: Any) -> _Call:
        return _Call(
            "GET",
            "/v1/dream/jobs",
            _jobs,
            params=self._query(scope, {"limit": limit, "offset": offset}),
        )

    def stats(self, **scope: Any) -> _Call:
        """Counts by status, decision tallies, token spend and estimated cost."""
        return _Call("GET", "/v1/dream/stats", _identity, params=self._query(scope))

    def consolidate(
        self,
        *,
        dry_run: bool = True,
        compact: bool = False,
        compact_min_memories: int | None = None,
        decay: bool | None = None,
        decay_after_days: int | None = None,
        decay_importance_floor: float | None = None,
        recall_epoch: Any = None,
        **scope: Any,
    ) -> _Call:
        """Retire memories that no longer earn their place — the nightly pass,
        run now for one user.

        **`dry_run` defaults to True**: it reports what would happen and changes
        nothing. Every override applies to this call only.
        """
        return _Call(
            "POST",
            "/v1/dream/consolidate",
            ConsolidationReport.from_dict,
            json=self._body(
                scope,
                {
                    "dryRun": dry_run,
                    "compact": compact,
                    "compactMinMemories": compact_min_memories,
                    "decay": decay,
                    "decayAfterDays": decay_after_days,
                    "decayImportanceFloor": decay_importance_floor,
                    "recallEpoch": _iso(recall_epoch),
                },
            ),
        )

    # --- server ---------------------------------------------------------------- #

    def health(self) -> _Call:
        """Liveness: the process is up. Touches no dependency."""
        return _Call("GET", "/health", _identity)

    def ready(self) -> _Call:
        """Readiness, with a check per dependency."""
        return _Call("GET", "/ready", _identity)

    def metrics(self) -> _Call:
        """Counters, latency histograms and token accounting for this process."""
        return _Call("GET", "/v1/metrics", _identity)

    def token_usage(self) -> _Call:
        """Token spend by operation, with an estimated cost, for this process."""
        return _Call("GET", "/v1/metrics/tokens", _identity)


_RETURNS: dict[str, Any] = {
    "add": AddResult,
    "create": MemoryDetail,
    "update": MemoryDetail,
    "forget": dict[str, Any],
    "purge": dict[str, Any],
    "search": SearchResult,
    "get": MemoryDetail,
    "list": list[MemoryRecord],
    "history": list[MemoryRecord],
    "audit": list[dict[str, Any]],
    "synthesize": DreamJob,
    "get_job": DreamJob,
    "list_jobs": list[DreamJob],
    "stats": dict[str, Any],
    "consolidate": ConsolidationReport,
    "health": dict[str, Any],
    "ready": dict[str, Any],
    "metrics": dict[str, Any],
    "token_usage": dict[str, Any],
}


def _signature_of(name: str) -> inspect.Signature:
    template = getattr(_Operations, name)
    return inspect.signature(template).replace(return_annotation=_RETURNS[name])


def _sync_operation(owner: str, name: str) -> Callable[..., Any]:
    template = getattr(_Operations, name)

    @functools.wraps(template)
    def method(self: Any, *args: Any, **kwargs: Any) -> Any:
        transport = self._transport
        return transport._send(getattr(transport._ops, name)(*args, **kwargs))

    method.__qualname__ = f"{owner}.{name}"
    method.__signature__ = _signature_of(name)  # type: ignore[attr-defined]
    return method


def _async_operation(owner: str, name: str) -> Callable[..., Any]:
    template = getattr(_Operations, name)

    @functools.wraps(template)
    async def method(self: Any, *args: Any, **kwargs: Any) -> Any:
        transport = self._transport
        return await transport._send(getattr(transport._ops, name)(*args, **kwargs))

    method.__qualname__ = f"{owner}.{name}"
    method.__signature__ = _signature_of(name)  # type: ignore[attr-defined]
    return method


def _sync_wait_for(
    self: Any, job_id: str, *, timeout: float = 300.0, poll_interval: float = 2.0, **scope: Any
) -> DreamJob:
    """Poll a Dream job until it finishes or `timeout` elapses."""
    transport = self._transport
    deadline = time.monotonic() + timeout
    while True:
        job = transport._send(transport._ops.get_job(job_id, **scope))
        if job.is_finished:
            return job
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Dream job {job_id} did not finish within {timeout}s (last status: {job.status})"
            )
        time.sleep(poll_interval)


async def _async_wait_for(
    self: Any, job_id: str, *, timeout: float = 300.0, poll_interval: float = 2.0, **scope: Any
) -> DreamJob:
    """Poll a Dream job until it finishes or `timeout` elapses."""
    transport = self._transport
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        job = await transport._send(transport._ops.get_job(job_id, **scope))
        if job.is_finished:
            return job
        if loop.time() >= deadline:
            raise TimeoutError(
                f"Dream job {job_id} did not finish within {timeout}s (last status: {job.status})"
            )
        await asyncio.sleep(poll_interval)


def _handle(response: httpx.Response) -> dict[str, Any]:
    request_id = response.headers.get("X-Request-ID")
    if response.status_code >= 400:
        try:
            payload = response.json()
        except ValueError:
            payload = {"error": {"message": response.text[:500]}}
        raise error_for(response.status_code, payload, request_id)
    if not response.content:
        return {}
    return response.json()


# --------------------------------------------------------------------------- #
# Namespaces
# --------------------------------------------------------------------------- #


class _MemoryAPI:
    """`client.memory.*` — remember and recall."""

    def __init__(self, transport: DreamClient) -> None:
        self._transport = transport


class _DreamAPI:
    """`client.dream.*` — synthesis, consolidation and jobs."""

    def __init__(self, transport: DreamClient) -> None:
        self._transport = transport


class _AsyncMemoryAPI:
    """`client.memory.*` — remember and recall."""

    def __init__(self, transport: AsyncDreamClient) -> None:
        self._transport = transport


class _AsyncDreamAPI:
    """`client.dream.*` — synthesis, consolidation and jobs."""

    def __init__(self, transport: AsyncDreamClient) -> None:
        self._transport = transport


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #


class DreamClient:
    """Synchronous client for a MemDream server.

    Create one per process and reuse it — the connection pool is shared. Also a
    context manager.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        *,
        api_key: str | None = None,
        user_id: str | None = None,
        phone_number: str | None = None,
        agent_id: str | None = None,
        client_id: str | None = None,
        namespace: dict[str, str] | None = None,
        bot_id: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        http_client: httpx.Client | None = None,
    ) -> None:
        # Sent per request rather than set on the client, so it also applies to
        # an `http_client` you supplied — where it used to be silently ignored,
        # surfacing as a 401 that looked like a bad key.
        self._headers = {"X-API-Key": api_key} if api_key else {}
        self._http = http_client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self._owns_http = http_client is None
        self._defaults = _ScopeDefaults(
            user_id=user_id,
            phone_number=phone_number,
            agent_id=agent_id,
            client_id=client_id,
            namespace=namespace,
            bot_id=bot_id,
        )
        self._ops = _Operations(self._defaults)
        self._transport = self

        self.memory = _MemoryAPI(self)
        self.dream = _DreamAPI(self)

    def _send(self, call: _Call) -> Any:
        try:
            response = self._http.request(
                call.method, call.path, params=call.params, json=call.json, headers=self._headers
            )
        except httpx.ConnectError as exc:
            raise DreamConnectionError(
                f"Cannot reach MemDream at {self._http.base_url}: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise DreamConnectionError(f"{call.method} {call.path} timed out: {exc}") from exc
        return call.parse(_handle(response))

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> DreamClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


class AsyncDreamClient:
    """Async client for a MemDream server. Same surface as `DreamClient`, awaited."""

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        *,
        api_key: str | None = None,
        user_id: str | None = None,
        phone_number: str | None = None,
        agent_id: str | None = None,
        client_id: str | None = None,
        namespace: dict[str, str] | None = None,
        bot_id: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._headers = {"X-API-Key": api_key} if api_key else {}
        self._http = http_client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout
        )
        self._owns_http = http_client is None
        self._defaults = _ScopeDefaults(
            user_id=user_id,
            phone_number=phone_number,
            agent_id=agent_id,
            client_id=client_id,
            namespace=namespace,
            bot_id=bot_id,
        )
        self._ops = _Operations(self._defaults)
        self._transport = self

        self.memory = _AsyncMemoryAPI(self)
        self.dream = _AsyncDreamAPI(self)

    async def _send(self, call: _Call) -> Any:
        try:
            response = await self._http.request(
                call.method, call.path, params=call.params, json=call.json, headers=self._headers
            )
        except httpx.ConnectError as exc:
            raise DreamConnectionError(
                f"Cannot reach MemDream at {self._http.base_url}: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise DreamConnectionError(f"{call.method} {call.path} timed out: {exc}") from exc
        return call.parse(_handle(response))

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> AsyncDreamClient:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()


# --------------------------------------------------------------------------- #
# Attach the operations
# --------------------------------------------------------------------------- #


def _attach(cls: type, names: tuple[str, ...], factory: Callable[[str, str], Any]) -> None:
    for name in names:
        setattr(cls, name, factory(cls.__name__, name))


_attach(_MemoryAPI, MEMORY_OPERATIONS, _sync_operation)
_attach(_DreamAPI, DREAM_OPERATIONS, _sync_operation)
_attach(DreamClient, MEMORY_OPERATIONS + DREAM_OPERATIONS + SERVER_OPERATIONS, _sync_operation)

_attach(_AsyncMemoryAPI, MEMORY_OPERATIONS, _async_operation)
_attach(_AsyncDreamAPI, DREAM_OPERATIONS, _async_operation)
_attach(
    AsyncDreamClient, MEMORY_OPERATIONS + DREAM_OPERATIONS + SERVER_OPERATIONS, _async_operation
)

_sync_wait_for.__name__ = _async_wait_for.__name__ = "wait_for"
for _cls in (_DreamAPI, DreamClient):
    _cls.wait_for = _sync_wait_for
for _cls in (_AsyncDreamAPI, AsyncDreamClient):
    _cls.wait_for = _async_wait_for
del _cls
