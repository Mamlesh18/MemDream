"""Sync and async clients.

Both expose the same three namespaces:

    client.memory.add / search / get / list / history
    client.dream.synthesize / get_job / wait_for / list_jobs / stats
    client.health()

Scope defaults (`client_id`, `bot_id`, `phone_number`) can be set once on the
constructor and omitted from every call, which is how an agent bound to one user
usually wants to use this.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from memdream.exceptions import DreamConnectionError, error_for
from memdream.models import AddResult, DreamJob, MemoryDetail, MemoryRecord, SearchResult

DEFAULT_TIMEOUT = 60.0


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
# Synchronous
# --------------------------------------------------------------------------- #


class DreamClient:
    """Synchronous client.

    Usable as a context manager, or long-lived — the underlying connection pool
    is reused, so creating one client per process is the intended pattern.
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
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key

        self._http = http_client or httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout
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

        self.memory = _MemoryAPI(self)
        self.dream = _DreamAPI(self)

    # --- transport ---------------------------------------------------------- #

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise DreamConnectionError(
                f"Cannot reach Dream Memory at {self._http.base_url}: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise DreamConnectionError(f"Request to {path} timed out: {exc}") from exc
        return _handle(response)

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def ready(self) -> dict[str, Any]:
        return self._request("GET", "/ready")

    def metrics(self) -> dict[str, Any]:
        return self._request("GET", "/v1/metrics")

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> DreamClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class _MemoryAPI:
    def __init__(self, client: DreamClient) -> None:
        self._client = client

    def add(
        self,
        prompt: str,
        *,
        client_id: str | None = None,
        bot_id: str | None = None,
        phone_number: str | None = None,
        user_id: str | None = None,
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        raw: bool = False,
    ) -> AddResult:
        """Remember a message.

        Set ``raw=True`` to store the text verbatim as one memory, skipping
        extraction but keeping deduplication and consolidation.
        """
        body: dict[str, Any] = {
            "prompt": prompt,
            **self._client._defaults.resolve(client_id, bot_id, phone_number, user_id),
        }
        if message_id:
            body["messageId"] = message_id
        if metadata:
            body["metadata"] = metadata
        if raw:
            body["raw"] = True
        return AddResult.from_dict(self._client._request("POST", "/v1/memories", json=body))

    def search(
        self,
        query: str,
        *,
        client_id: str | None = None,
        bot_id: str | None = None,
        phone_number: str | None = None,
        user_id: str | None = None,
        limit: int = 10,
        latest_only: bool = False,
        include_superseded: bool = True,
        include_merged: bool = False,
        memory_types: list[str] | None = None,
        min_score: float | None = None,
    ) -> SearchResult:
        """Search memories.

        ``latest_only=True`` answers "what is true now?"; the default also
        returns superseded history, ranked below current facts.
        """
        body: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "latestOnly": latest_only,
            "includeSuperseded": include_superseded,
            "includeMerged": include_merged,
            **self._client._defaults.resolve(client_id, bot_id, phone_number, user_id),
        }
        if memory_types:
            body["memoryTypes"] = memory_types
        if min_score is not None:
            body["minScore"] = min_score
        return SearchResult.from_dict(
            self._client._request("POST", "/v1/memories/search", json=body)
        )

    def get(
        self,
        memory_id: str,
        *,
        client_id: str | None = None,
        bot_id: str | None = None,
        phone_number: str | None = None,
        user_id: str | None = None,
    ) -> MemoryDetail:
        params = self._client._defaults.resolve(client_id, bot_id, phone_number, user_id)
        params.pop("namespace", None)  # query strings carry flat values only
        return MemoryDetail.from_dict(
            self._client._request("GET", f"/v1/memories/{memory_id}", params=params)
        )

    # --- manual management ------------------------------------------------- #

    def create(
        self,
        content: str,
        *,
        memory_type: str = "other",
        importance: float = 0.6,
        confidence: float = 1.0,
        explicit: bool = True,
        temporal_nature: str = "current",
        metadata: dict[str, Any] | None = None,
        allow_duplicate: bool = False,
        **scope,
    ) -> MemoryDetail:
        """Write a memory verbatim, bypassing extraction and Dream reasoning.

        Use `add()` for conversational input, where the system decides what is
        worth keeping. Use this when you already know exactly what to store —
        importing profile data, or recording something the extraction policy
        would reject. Write third-person text: "User is allergic to peanuts."
        """
        body: dict[str, Any] = {
            "content": content,
            "memoryType": memory_type,
            "importance": importance,
            "confidence": confidence,
            "explicit": explicit,
            "temporalNature": temporal_nature,
            "allowDuplicate": allow_duplicate,
            **self._client._defaults.resolve(
                scope.get("client_id"), scope.get("bot_id"),
                scope.get("phone_number"), scope.get("user_id"),
            ),
        }
        if metadata:
            body["metadata"] = metadata
        return MemoryDetail.from_dict(
            self._client._request("POST", "/v1/memories/manual", json=body)
        )

    def update(self, memory_id: str, **changes) -> MemoryDetail:
        """Correct an active memory in place. Only supplied fields change.

        The memory keeps its id. Superseded and merged memories are history and
        cannot be edited — that raises `DreamAPIError` with status 409.
        """
        field_map = {
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
        body: dict[str, Any] = {
            wire: changes[name] for name, wire in field_map.items() if name in changes
        }
        if not body:
            raise ValueError("Supply at least one field to update")

        body.update(self._client._defaults.resolve(
            changes.get("client_id"), changes.get("bot_id"),
            changes.get("phone_number"), changes.get("user_id"),
        ))
        return MemoryDetail.from_dict(
            self._client._request("PATCH", f"/v1/memories/{memory_id}", json=body)
        )

    def forget(self, memory_id: str, *, permanent: bool = False, **scope) -> dict[str, Any]:
        """Retire a memory, or erase it.

        Default retires it: the row is kept and leaves the search index, so it
        stops influencing answers but stays auditable. `permanent=True` erases
        it outright and cannot be undone.
        """
        params = {
            **self._client._defaults.resolve(
                scope.get("client_id"), scope.get("bot_id"),
                scope.get("phone_number"), scope.get("user_id"),
            ),
            "permanent": str(permanent).lower(),
        }
        return self._client._request("DELETE", f"/v1/memories/{memory_id}", params=params)

    def purge(self, *, confirm: bool = False, **scope) -> dict[str, Any]:
        """Erase everything for one user, audit trail included. Irreversible.

        Requires ``confirm=True``; the flag exists so this cannot happen by
        typo or by an argument arriving as None.
        """
        if confirm is not True:
            raise ValueError(
                "purge() erases all of this user's memory and cannot be undone. "
                "Pass confirm=True if that is what you intend."
            )
        params = {
            **self._client._defaults.resolve(
                scope.get("client_id"), scope.get("bot_id"),
                scope.get("phone_number"), scope.get("user_id"),
            ),
            "confirm": "ERASE",
        }
        return self._client._request("DELETE", "/v1/memories", params=params)

    def list(
        self,
        *,
        client_id: str | None = None,
        bot_id: str | None = None,
        phone_number: str | None = None,
        user_id: str | None = None,
        status: list[str] | None = None,
        memory_type: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MemoryRecord]:
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            **self._client._defaults.resolve(client_id, bot_id, phone_number, user_id),
        }
        if status:
            params["status"] = status
        if memory_type:
            params["memoryType"] = memory_type
        data = self._client._request("GET", "/v1/memories", params=params)
        return [MemoryRecord.from_dict(item) for item in data.get("memories", [])]

    def history(
        self,
        memory_id: str,
        *,
        client_id: str | None = None,
        bot_id: str | None = None,
        phone_number: str | None = None,
        user_id: str | None = None,
    ) -> list[MemoryRecord]:
        """Walk the supersede chain back to the original fact."""
        params = self._client._defaults.resolve(client_id, bot_id, phone_number, user_id)
        params.pop("namespace", None)  # query strings carry flat values only
        data = self._client._request("GET", f"/v1/memories/{memory_id}/history", params=params)
        return [MemoryRecord.from_dict(item) for item in data.get("memories", [])]

    def audit(
        self,
        *,
        client_id: str | None = None,
        bot_id: str | None = None,
        phone_number: str | None = None,
        user_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params = {
            "limit": limit,
            **self._client._defaults.resolve(client_id, bot_id, phone_number, user_id),
        }
        data = self._client._request("GET", "/v1/memories/audit/operations", params=params)
        return data.get("operations", [])


class _DreamAPI:
    def __init__(self, client: DreamClient) -> None:
        self._client = client

    def synthesize(
        self,
        *,
        client_id: str | None = None,
        bot_id: str | None = None,
        phone_number: str | None = None,
        user_id: str | None = None,
        wait: bool = False,
        force: bool = False,
    ) -> DreamJob:
        """Queue a synthesis run.

        ``wait=True`` runs it inline on the server and returns the finished job.
        Convenient in scripts; not something to do on a request path.
        """
        body: dict[str, Any] = {
            "wait": wait,
            "force": force,
            **self._client._defaults.resolve(client_id, bot_id, phone_number, user_id),
        }
        return DreamJob.from_dict(
            self._client._request("POST", "/v1/dream/synthesize", json=body)
        )

    def get_job(
        self,
        job_id: str,
        *,
        client_id: str | None = None,
        bot_id: str | None = None,
        phone_number: str | None = None,
        user_id: str | None = None,
    ) -> DreamJob:
        params = self._client._defaults.resolve(client_id, bot_id, phone_number, user_id)
        params.pop("namespace", None)  # query strings carry flat values only
        return DreamJob.from_dict(
            self._client._request("GET", f"/v1/dream/jobs/{job_id}", params=params)
        )

    def wait_for(
        self,
        job_id: str,
        *,
        timeout: float = 300.0,
        poll_interval: float = 2.0,
        **scope,
    ) -> DreamJob:
        """Poll until the job finishes or ``timeout`` elapses."""
        deadline = time.monotonic() + timeout
        while True:
            job = self.get_job(job_id, **scope)
            if job.is_finished:
                return job
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Dream job {job_id} did not finish within {timeout}s "
                    f"(last status: {job.status})"
                )
            time.sleep(poll_interval)

    def list_jobs(self, *, limit: int = 20, **scope) -> list[DreamJob]:
        params = {"limit": limit, **self._client._defaults.resolve(
            scope.get("client_id"), scope.get("bot_id"),
            scope.get("phone_number"), scope.get("user_id"),
        )}
        data = self._client._request("GET", "/v1/dream/jobs", params=params)
        return [DreamJob.from_dict(item) for item in data.get("jobs", [])]

    def stats(self, **scope) -> dict[str, Any]:
        params = self._client._defaults.resolve(
            scope.get("client_id"), scope.get("bot_id"),
            scope.get("phone_number"), scope.get("user_id"),
        )
        return self._client._request("GET", "/v1/dream/stats", params=params)


# --------------------------------------------------------------------------- #
# Asynchronous
# --------------------------------------------------------------------------- #


class AsyncDreamClient:
    """Async client. Same surface as `DreamClient`, with awaitable methods."""

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
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key

        self._http = http_client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout
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

        self.memory = _AsyncMemoryAPI(self)
        self.dream = _AsyncDreamAPI(self)

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise DreamConnectionError(
                f"Cannot reach Dream Memory at {self._http.base_url}: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise DreamConnectionError(f"Request to {path} timed out: {exc}") from exc
        return _handle(response)

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def ready(self) -> dict[str, Any]:
        return await self._request("GET", "/ready")

    async def metrics(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/metrics")

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> AsyncDreamClient:
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()


class _AsyncMemoryAPI:
    """Mirrors `_MemoryAPI` method for method."""

    def __init__(self, client: AsyncDreamClient) -> None:
        self._client = client

    def _scope(self, kwargs: dict[str, Any]) -> dict[str, str]:
        return self._client._defaults.resolve(
            kwargs.get("client_id"),
            kwargs.get("bot_id"),
            kwargs.get("phone_number"),
            kwargs.get("user_id"),
        )

    async def add(self, prompt: str, **kwargs) -> AddResult:
        body: dict[str, Any] = {"prompt": prompt, **self._scope(kwargs)}
        if kwargs.get("message_id"):
            body["messageId"] = kwargs["message_id"]
        if kwargs.get("metadata"):
            body["metadata"] = kwargs["metadata"]
        if kwargs.get("raw"):
            body["raw"] = True
        return AddResult.from_dict(await self._client._request("POST", "/v1/memories", json=body))

    async def search(self, query: str, **kwargs) -> SearchResult:
        body: dict[str, Any] = {
            "query": query,
            "limit": kwargs.get("limit", 10),
            "latestOnly": kwargs.get("latest_only", False),
            "includeSuperseded": kwargs.get("include_superseded", True),
            "includeMerged": kwargs.get("include_merged", False),
            **self._scope(kwargs),
        }
        if kwargs.get("memory_types"):
            body["memoryTypes"] = kwargs["memory_types"]
        if kwargs.get("min_score") is not None:
            body["minScore"] = kwargs["min_score"]
        return SearchResult.from_dict(
            await self._client._request("POST", "/v1/memories/search", json=body)
        )

    async def get(self, memory_id: str, **kwargs) -> MemoryDetail:
        return MemoryDetail.from_dict(
            await self._client._request(
                "GET", f"/v1/memories/{memory_id}", params=self._scope(kwargs)
            )
        )

    async def create(
        self,
        content: str,
        *,
        memory_type: str = "other",
        importance: float = 0.6,
        confidence: float = 1.0,
        explicit: bool = True,
        temporal_nature: str = "current",
        metadata: dict[str, Any] | None = None,
        allow_duplicate: bool = False,
        **kwargs,
    ) -> MemoryDetail:
        """Write a memory verbatim, bypassing extraction and Dream reasoning."""
        body: dict[str, Any] = {
            "content": content,
            "memoryType": memory_type,
            "importance": importance,
            "confidence": confidence,
            "explicit": explicit,
            "temporalNature": temporal_nature,
            "allowDuplicate": allow_duplicate,
            **self._scope(kwargs),
        }
        if metadata:
            body["metadata"] = metadata
        return MemoryDetail.from_dict(
            await self._client._request("POST", "/v1/memories/manual", json=body)
        )

    async def update(self, memory_id: str, **changes) -> MemoryDetail:
        """Correct an active memory in place. Only supplied fields change."""
        field_map = {
            "content": "content", "memory_type": "memoryType",
            "importance": "importance", "confidence": "confidence",
            "explicit": "explicit", "temporal_nature": "temporalNature",
            "valid_from": "validFrom", "valid_until": "validUntil",
            "metadata": "metadata",
        }
        body: dict[str, Any] = {
            wire: changes[name] for name, wire in field_map.items() if name in changes
        }
        if not body:
            raise ValueError("Supply at least one field to update")
        body.update(self._scope(changes))
        return MemoryDetail.from_dict(
            await self._client._request("PATCH", f"/v1/memories/{memory_id}", json=body)
        )

    async def forget(self, memory_id: str, *, permanent: bool = False, **kwargs) -> dict[str, Any]:
        """Retire a memory, or erase it with ``permanent=True``."""
        params = {**self._scope(kwargs), "permanent": str(permanent).lower()}
        return await self._client._request("DELETE", f"/v1/memories/{memory_id}", params=params)

    async def purge(self, *, confirm: bool = False, **kwargs) -> dict[str, Any]:
        """Erase everything for one user, audit trail included. Irreversible."""
        if confirm is not True:
            raise ValueError(
                "purge() erases all of this user's memory and cannot be undone. "
                "Pass confirm=True if that is what you intend."
            )
        params = {**self._scope(kwargs), "confirm": "ERASE"}
        return await self._client._request("DELETE", "/v1/memories", params=params)

    async def list(self, *, limit: int = 50, offset: int = 0, **kwargs) -> list[MemoryRecord]:
        params: dict[str, Any] = {"limit": limit, "offset": offset, **self._scope(kwargs)}
        if kwargs.get("status"):
            params["status"] = kwargs["status"]
        if kwargs.get("memory_type"):
            params["memoryType"] = kwargs["memory_type"]
        data = await self._client._request("GET", "/v1/memories", params=params)
        return [MemoryRecord.from_dict(item) for item in data.get("memories", [])]

    async def history(self, memory_id: str, **kwargs) -> list[MemoryRecord]:
        data = await self._client._request(
            "GET", f"/v1/memories/{memory_id}/history", params=self._scope(kwargs)
        )
        return [MemoryRecord.from_dict(item) for item in data.get("memories", [])]

    async def audit(self, *, limit: int = 50, **kwargs) -> list[dict[str, Any]]:
        params = {"limit": limit, **self._scope(kwargs)}
        data = await self._client._request("GET", "/v1/memories/audit/operations", params=params)
        return data.get("operations", [])


class _AsyncDreamAPI:
    def __init__(self, client: AsyncDreamClient) -> None:
        self._client = client

    def _scope(self, kwargs: dict[str, Any]) -> dict[str, str]:
        return self._client._defaults.resolve(
            kwargs.get("client_id"),
            kwargs.get("bot_id"),
            kwargs.get("phone_number"),
            kwargs.get("user_id"),
        )

    async def synthesize(self, *, wait: bool = False, force: bool = False, **kwargs) -> DreamJob:
        body: dict[str, Any] = {"wait": wait, "force": force, **self._scope(kwargs)}
        return DreamJob.from_dict(
            await self._client._request("POST", "/v1/dream/synthesize", json=body)
        )

    async def get_job(self, job_id: str, **kwargs) -> DreamJob:
        return DreamJob.from_dict(
            await self._client._request(
                "GET", f"/v1/dream/jobs/{job_id}", params=self._scope(kwargs)
            )
        )

    async def wait_for(
        self,
        job_id: str,
        *,
        timeout: float = 300.0,
        poll_interval: float = 2.0,
        **scope,
    ) -> DreamJob:
        """Poll until the job finishes or ``timeout`` elapses."""
        import asyncio

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            job = await self.get_job(job_id, **scope)
            if job.is_finished:
                return job
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"Dream job {job_id} did not finish within {timeout}s "
                    f"(last status: {job.status})"
                )
            await asyncio.sleep(poll_interval)

    async def list_jobs(self, *, limit: int = 20, **kwargs) -> list[DreamJob]:
        params = {"limit": limit, **self._scope(kwargs)}
        data = await self._client._request("GET", "/v1/dream/jobs", params=params)
        return [DreamJob.from_dict(item) for item in data.get("jobs", [])]

    async def stats(self, **kwargs) -> dict[str, Any]:
        return await self._client._request("GET", "/v1/dream/stats", params=self._scope(kwargs))
