"""MemDream with no server.

    from memdream import MemDream

    mem = MemDream(user_id="alice")
    mem.add("I moved to Bangalore last month.")
    print(mem.search("where do they live?").as_context())

That is the whole integration. No uvicorn, no worker, no HTTP — the same
`DreamContainer` the API builds, called directly in your process.

## Why this exists alongside `DreamClient`

`DreamClient` talks to a running MemDream over HTTP. `MemDream` *is* MemDream.
The two expose **the same method names and return the same objects**, so the
line that changes when you move from a prototype to a deployed service is the
constructor and nothing else:

    mem = MemDream(user_id="alice")                              # embedded
    mem = DreamClient("https://memory.internal", user_id="alice")  # hosted

Both also answer to `.memory` and `.dream`, so code written against the
namespaced client form runs unchanged either way.

## Sync and async

The core is async throughout. `AsyncMemDream` is the direct facade — use it
inside an async agent, where it adds one `await` and no threads.

`MemDream` is the synchronous one, and it is not `asyncio.run` per call: the
database driver holds connections bound to a loop, so a fresh loop each time
would break them on the second call. Instead it owns one background thread
running one loop for its whole lifetime, and submits coroutines to it. That is
invisible from outside, and it means a script, a notebook or a Django view can
use MemDream without knowing the core is async at all.

## Lifetime

The container holds a database connection, an embedding client and an LLM
client. Build one `MemDream` per process and keep it — or use it as a context
manager, which closes everything on exit:

    with MemDream(user_id="alice") as mem:
        mem.add("I prefer aisle seats.")

Constructing one per request works but pays connection setup every time, and
will exhaust the database's connection limit under load.
"""

from __future__ import annotations

import asyncio
import functools
import threading
from concurrent.futures import Future
from datetime import datetime
from typing import TYPE_CHECKING, Any

from memdream.models import (
    AddResult,
    ConsolidationReport,
    DreamJob,
    MemoryDetail,
    MemoryRecord,
    SearchResult,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance only
    from app.container import DreamContainer
    from app.core.config import Settings
    from app.models.scope import MemoryScope


def _require_core() -> None:
    """Fail with an instruction rather than a bare ImportError.

    `DreamClient` needs only `httpx`, so it is reasonable to vendor the client
    module alone next to an agent framework. Anyone who does that and then
    reaches for `MemDream` should be told what is missing, not shown a traceback
    about `app.container` — a module they never heard of and did not import.
    """
    try:
        import app.container  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on install shape
        raise ImportError(
            "The embedded MemDream runs the engine in your process, so it needs "
            "the whole package: pip install memdream\n"
            "To talk to a MemDream server over HTTP instead, use DreamClient, "
            "which needs only httpx."
        ) from exc


# --------------------------------------------------------------------------- #
# Async facade
# --------------------------------------------------------------------------- #


class AsyncMemDream:
    """MemDream in-process, async.

    Every method mirrors `AsyncDreamClient` — same names, same arguments, same
    return types — so the two are interchangeable.
    """

    def __init__(
        self,
        *,
        user_id: str | None = None,
        phone_number: str | None = None,
        agent_id: str | None = None,
        client_id: str | None = None,
        namespace: dict[str, str] | None = None,
        bot_id: str | None = None,
        settings: Settings | None = None,
        container: DreamContainer | None = None,
        database: Any = None,
        **overrides: Any,
    ) -> None:
        """Scope defaults are set once here and omitted from every call.

        Configuration comes from the environment unless you say otherwise:

            settings    a fully-built `Settings`
            **overrides a shortcut for one or two fields (`llm_provider="fake"`)
            database    an already-connected store, when you do not want the
                        facade to open its own — a throwaway schema in a test,
                        or a connection the surrounding app already owns
            container   an already-built container, adopted whole. This is how a
                        FastAPI app hands its own wiring to code that wants the
                        SDK surface, and the facade will not close what it did
                        not open
        """
        _require_core()

        self._settings = settings
        self._overrides = overrides
        self._database = database
        self._container = container
        # Adopted containers belong to their owner; ours are ours to close.
        self._owns_container = container is None

        self._defaults = {
            "user_id": user_id,
            "phone_number": phone_number,
            "agent_id": agent_id or bot_id,
            "client_id": client_id,
            "namespace": namespace or {},
        }
        self._lock = asyncio.Lock()

        # Same object, two names: code written against the namespaced client
        # form (`client.memory.add`, `client.dream.synthesize`) runs unchanged.
        # The two method sets do not overlap, so one object can serve both.
        self.memory = self
        self.dream = self

    # --- lifecycle ------------------------------------------------------------ #

    async def start(self) -> DreamContainer:
        """Build the container. Called automatically on first use.

        Lazy rather than eager because construction opens connections and may
        probe the LLM provider, and `MemDream(user_id=...)` at module import
        time should not do either.
        """
        if self._container is not None:
            return self._container

        async with self._lock:
            if self._container is None:
                from app.container import DreamContainer
                from app.core.config import Settings, get_settings

                settings = self._settings
                if settings is None:
                    settings = (
                        Settings(**self._overrides) if self._overrides else get_settings()
                    )
                elif self._overrides:
                    settings = settings.model_copy(update=self._overrides)

                self._container = await DreamContainer.create(
                    settings, database=self._database
                )
        return self._container

    async def close(self) -> None:
        if self._container is not None and self._owns_container:
            await self._container.close()
        self._container = None

    async def __aenter__(self) -> AsyncMemDream:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    @property
    def container(self) -> DreamContainer:
        """The wired container, for anything the facade does not cover.

        An escape hatch on purpose: the facade is the common path, not a wall.
        Raises if called before the first operation — use `await start()` first.
        """
        if self._container is None:
            raise RuntimeError("Not started yet. Call `await start()` or use `async with`.")
        return self._container

    # --- scope ---------------------------------------------------------------- #

    async def _scope(self, kwargs: dict[str, Any]) -> MemoryScope:
        """Merge per-call scope arguments over the constructor defaults.

        Built through `build_scope` rather than `MemoryScope(...)` directly, so
        the embedded path derives a user id from a phone number exactly the way
        the HTTP path does — same salt, same digest, same partition.

        An unrecognised keyword raises rather than being ignored: a misspelt
        `clinet_id` quietly dropped would read from a different partition than
        the caller named.
        """
        from app.core.identity import build_scope

        unknown = set(kwargs) - {
            "user_id", "phone_number", "agent_id", "client_id", "namespace", "bot_id"
        }
        if unknown:
            raise TypeError(
                f"Unexpected argument(s): {', '.join(sorted(unknown))}. Scope arguments "
                "are: user_id, phone_number, agent_id, client_id, namespace, bot_id"
            )

        container = await self.start()
        merged = {
            **self._defaults,
            **{k: v for k, v in kwargs.items() if v is not None},
        }
        namespace = {**self._defaults["namespace"], **(kwargs.get("namespace") or {})}
        if not merged.get("user_id") and not merged.get("phone_number"):
            raise ValueError(
                "Either user_id or phone_number must be supplied, on the call or "
                "as a client default"
            )
        return build_scope(
            user_id=merged.get("user_id"),
            phone_number=merged.get("phone_number"),
            agent_id=merged.get("agent_id") or merged.get("bot_id"),
            client_id=merged.get("client_id"),
            namespace=namespace,
            settings=container.settings,
        )

    # --- remember ------------------------------------------------------------- #

    async def add(
        self,
        prompt: str,
        *,
        message_id: str | None = None,
        timestamp: datetime | None = None,
        metadata: dict[str, Any] | None = None,
        raw: bool = False,
        **scope_kwargs: Any,
    ) -> AddResult:
        """Extract memories from a message and let Dream decide what to do.

        `raw=True` skips extraction and stores the text verbatim as one memory,
        while still running the ADD/SUPERSEDE/MERGE decision against it.
        """
        if not prompt or len(prompt) > 20000:
            from memdream.exceptions import DreamValidationError

            raise DreamValidationError(
                "prompt must be between 1 and 20000 characters",
                status_code=422,
                code="validation_error",
            )

        container = await self.start()
        scope = await self._scope(scope_kwargs)

        outcome = await container.memory_service.add(
            scope,
            prompt,
            message_id=message_id,
            timestamp=timestamp,
            metadata=metadata,
            raw=raw,
        )
        # Synthesis fires on accumulated material rather than a timer, exactly as
        # the HTTP route does — otherwise an embedded deployment would never
        # produce patterns at all.
        await container.dream_service.maybe_trigger(scope)

        primary = outcome.primary
        return AddResult.from_dict(
            {
                "action": primary.action.value if primary else "NOOP",
                "memoryId": primary.memory_id if primary else None,
                "supersededMemoryIds": primary.superseded_memory_ids if primary else [],
                "mergedMemoryIds": primary.merged_memory_ids if primary else [],
                "results": [
                    {
                        "action": result.action.value,
                        "memoryId": result.memory_id,
                        "content": result.memory.content if result.memory else None,
                        "reason": result.reason,
                        "supersededMemoryIds": result.superseded_memory_ids,
                        "mergedMemoryIds": result.merged_memory_ids,
                        "operationId": result.operation_id,
                    }
                    for result in outcome.results
                ],
                "extractedCount": outcome.extracted_count,
            }
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
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        metadata: dict[str, Any] | None = None,
        allow_duplicate: bool = False,
        **scope_kwargs: Any,
    ) -> MemoryDetail:
        """Write a memory verbatim, bypassing extraction and Dream reasoning.

        `add` decides *what* is worth remembering. This is the deliberate path:
        importing known facts, or storing something the extraction policy would
        have rejected. Write content in the third person, matching what the
        extractor produces, so retrieval treats it the same way.
        """
        container = await self.start()
        scope = await self._scope(scope_kwargs)

        memory, _created = await container.memory_service.create_manual(
            scope,
            content=content,
            memory_type=memory_type,
            importance=importance,
            confidence=confidence,
            explicit=explicit,
            temporal_nature=temporal_nature,
            valid_from=valid_from,
            valid_until=valid_until,
            metadata=metadata,
            allow_duplicate=allow_duplicate,
        )
        return MemoryDetail.from_dict({"memory": memory.public_dump()})

    async def update(self, memory_id: str, **changes: Any) -> MemoryDetail:
        """Correct an active memory in place. Only the fields you pass change.

        The memory keeps its id, so anything referencing it stays valid. Only
        `active` memories can be edited — superseded and merged rows are
        history, and history is not rewritten.
        """
        container = await self.start()
        scope_kwargs = {
            key: changes.pop(key)
            for key in ("user_id", "phone_number", "agent_id", "client_id", "namespace", "bot_id")
            if key in changes
        }
        if not changes:
            raise ValueError("Supply at least one field to update")
        scope = await self._scope(scope_kwargs)

        memory = await container.memory_service.update_manual(
            scope, memory_id, changes=changes
        )
        if memory is None:
            from memdream.exceptions import DreamNotFoundError

            raise DreamNotFoundError(f"Memory {memory_id} not found")
        return MemoryDetail.from_dict({"memory": memory.public_dump()})

    async def forget(
        self, memory_id: str, *, permanent: bool = False, **scope_kwargs: Any
    ) -> dict[str, Any]:
        """Retire a memory, or erase it.

        The default is reversible: the memory moves to `expired` and leaves the
        search index, so it stops influencing answers but stays auditable. This
        is what "forget that" should do. `permanent=True` erases the row and
        every edge touching it, and cannot be undone.
        """
        container = await self.start()
        scope = await self._scope(scope_kwargs)

        _memory, found = await container.memory_service.forget_memory(
            scope, memory_id, permanent=permanent
        )
        if not found:
            from memdream.exceptions import DreamNotFoundError

            raise DreamNotFoundError(f"Memory {memory_id} not found")
        return {
            "memoryId": memory_id,
            "result": "deleted" if permanent else "expired",
            "permanent": permanent,
        }

    async def purge(self, *, confirm: bool = False, **scope_kwargs: Any) -> dict[str, Any]:
        """Erase everything for one user: memories, edges, audit trail, jobs.

        `confirm=True` is required. Irreversible, and the audit trail goes too —
        retaining "we decided to store X about this user" after deleting X would
        defeat an erasure request.
        """
        if not confirm:
            raise ValueError("purge() erases a user's entire memory. Pass confirm=True.")

        container = await self.start()
        scope = await self._scope(scope_kwargs)
        counts = await container.memory_service.purge_user(scope)
        # The same field names `DELETE /v1/memories` returns, so code reading the
        # result does not care which mode produced it.
        return {
            "deletedMemories": counts.get("memories", 0),
            "deletedRelations": counts.get("relations", 0),
            "deletedOperations": counts.get("operations", 0),
            "deletedJobs": counts.get("jobs", 0),
            "operationId": None,
        }

    # --- recall ---------------------------------------------------------------- #

    async def search(
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
        as_of: datetime | None = None,
        **scope_kwargs: Any,
    ) -> SearchResult:
        """Hybrid-ranked semantic search: meaning, importance, confidence,
        recency and status, not similarity alone.

        `as_of` makes it a point-in-time query — "what was true on 1 March?" —
        which is the payoff for keeping superseded memories instead of deleting
        them.
        """
        from app.models.requests import SearchMemoryRequest

        container = await self.start()
        scope = await self._scope(scope_kwargs)

        scored = await container.memory_service.search(
            scope,
            SearchMemoryRequest(
                user_id=scope.user_id,
                query=query,
                limit=limit,
                latest_only=latest_only,
                include_superseded=include_superseded,
                include_merged=include_merged,
                include_expired=include_expired,
                memory_types=memory_types or [],
                min_score=min_score,
                as_of=as_of,
            ),
        )
        return SearchResult.from_dict(
            {"memories": [item.public_dump() for item in scored], "count": len(scored)}
        )

    async def get(self, memory_id: str, **scope_kwargs: Any) -> MemoryDetail:
        """One memory with its relationships, sources and what derives from it."""
        container = await self.start()
        scope = await self._scope(scope_kwargs)

        detail = await container.memory_service.get_detail(scope, memory_id)
        if detail is None:
            from memdream.exceptions import DreamNotFoundError

            raise DreamNotFoundError(f"Memory {memory_id} not found")

        return MemoryDetail.from_dict(
            {
                "memory": detail.memory.public_dump(),
                "relationships": [row.model_dump(by_alias=True) for row in detail.relationships],
                "sourceMemories": [
                    row.model_dump(by_alias=True) for row in detail.source_memories
                ],
                "derivedMemories": [
                    row.model_dump(by_alias=True) for row in detail.derived_memories
                ],
                "supersededBy": detail.memory.superseded_by,
                "mergedInto": detail.memory.merged_into,
            }
        )

    async def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: list[str] | None = None,
        memory_type: list[str] | None = None,
        **scope_kwargs: Any,
    ) -> list[MemoryRecord]:
        """Browse without a semantic query. Active memories only by default."""
        from app.models.enums import MemoryStatus

        container = await self.start()
        scope = await self._scope(scope_kwargs)

        statuses = (
            [MemoryStatus(value) for value in status] if status else [MemoryStatus.ACTIVE]
        )
        memories, _total = await container.memory_service.list_memories(
            scope,
            statuses=statuses,
            memory_types=memory_type,
            limit=limit,
            offset=offset,
        )
        return [MemoryRecord.from_dict(memory.public_dump()) for memory in memories]

    async def history(self, memory_id: str, **scope_kwargs: Any) -> list[MemoryRecord]:
        """Follow the supersede chain back to the original fact.

        Answers "when did this change, and what did it say before?" — the reason
        superseded memories are retained rather than deleted.
        """
        container = await self.start()
        scope = await self._scope(scope_kwargs)

        chain = await container.memory_service.history(scope, memory_id)
        if not chain:
            from memdream.exceptions import DreamNotFoundError

            raise DreamNotFoundError(f"Memory {memory_id} not found")
        return [MemoryRecord.from_dict(memory.public_dump()) for memory in chain]

    async def audit(
        self,
        *,
        memory_id: str | None = None,
        action: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
        **scope_kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Every Dream decision for this user, NOOPs included — or, with
        `memory_id`, every decision that touched that one memory.

        The NOOPs are the useful ones when nothing is being remembered and you
        need to find out why. `action` and `offset` apply to the user-wide log
        only.
        """
        container = await self.start()
        scope = await self._scope(scope_kwargs)

        if memory_id is not None:
            # Scope-check first: the audit trail is keyed by memory id, so
            # reading it directly would bypass the tenancy filter.
            if await container.memories.get(scope, memory_id) is None:
                from memdream.exceptions import DreamNotFoundError

                raise DreamNotFoundError(f"Memory {memory_id} not found")
            operations = await container.audit.list_for_memory(memory_id, limit=limit)
        else:
            operations = await container.audit.list_for_scope(
                scope, limit=limit, offset=offset, actions=action
            )
        return [op.model_dump(by_alias=True, mode="json") for op in operations]

    # --- dream ----------------------------------------------------------------- #

    async def synthesize(
        self,
        *,
        wait: bool = False,
        force: bool = False,
        min_memories: int | None = None,
        **scope_kwargs: Any,
    ) -> DreamJob:
        """Look for patterns across this user's memories.

        Embedded, `wait=True` is usually what you want: there is no worker
        process, so nothing else will pick the job up.
        """
        container = await self.start()
        scope = await self._scope(scope_kwargs)

        job = await container.dream_service.request_synthesis(
            scope,
            wait=wait,
            force=force,
            min_memories=min_memories,
            requested_by="embedded-sdk",
        )
        return DreamJob.from_dict(job.public_dump())

    async def get_job(self, job_id: str, **scope_kwargs: Any) -> DreamJob:
        container = await self.start()
        scope = await self._scope(scope_kwargs)
        job = await container.dream_service.get_job(job_id, scope)
        return DreamJob.from_dict(job.public_dump())

    async def wait_for(
        self,
        job_id: str,
        *,
        timeout: float = 300.0,
        poll_interval: float = 2.0,
        **scope_kwargs: Any,
    ) -> DreamJob:
        """Poll until the job finishes or ``timeout`` elapses.

        Present for parity with `DreamClient`. Embedded you rarely need it:
        `synthesize(wait=True)` runs the job in this process and returns it
        finished, whereas over HTTP the work happens in a worker you can only
        observe by polling.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            job = await self.get_job(job_id, **scope_kwargs)
            if job.is_finished:
                return job
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    f"Dream job {job_id} did not finish within {timeout}s "
                    f"(last status: {job.status})"
                )
            await asyncio.sleep(poll_interval)

    async def list_jobs(
        self, *, limit: int = 20, offset: int = 0, **scope_kwargs: Any
    ) -> list[DreamJob]:
        container = await self.start()
        scope = await self._scope(scope_kwargs)
        jobs = await container.dream_service.list_jobs(scope, limit=limit, offset=offset)
        return [DreamJob.from_dict(job.public_dump()) for job in jobs]

    async def consolidate(
        self,
        *,
        dry_run: bool = True,
        compact: bool = False,
        compact_min_memories: int | None = None,
        decay: bool | None = None,
        decay_after_days: int | None = None,
        decay_importance_floor: float | None = None,
        recall_epoch: Any = None,
        **scope_kwargs: Any,
    ) -> ConsolidationReport:
        """Retire memories that no longer earn their place — the nightly pass,
        run now for one user.

        **`dry_run` defaults to True**: it reports what would happen and changes
        nothing. Nothing is ever deleted either way — retiring is a status change
        to `expired`, so a wrong call costs a flip rather than a row.

        Every override applies to this call only and never changes what the
        nightly pass does. Decay needs `recall_epoch` before it can fire at all.
        """
        from app.dream.consolidation import run_consolidation_pass

        container = await self.start()
        scope = await self._scope(scope_kwargs)
        if hasattr(recall_epoch, "isoformat"):
            recall_epoch = recall_epoch.isoformat()

        report = await run_consolidation_pass(
            consolidation=container.consolidation,
            compaction=container.compaction,
            settings=container.settings,
            scope=scope,
            dry_run=dry_run,
            compact=compact,
            compact_min_memories=compact_min_memories,
            decay=decay,
            decay_after_days=decay_after_days,
            decay_importance_floor=decay_importance_floor,
            recall_epoch=recall_epoch,
        )
        return ConsolidationReport.from_dict(report)

    async def stats(self, **scope_kwargs: Any) -> dict[str, Any]:
        """Counts by status, token spend and estimated cost for this user."""
        container = await self.start()
        scope = await self._scope(scope_kwargs)
        return await container.memory_service.stats(scope)

    async def health(self) -> dict[str, Any]:
        """Per-dependency health, the same structure `/ready` returns."""
        container = await self.start()
        return await container.health()


# --------------------------------------------------------------------------- #
# Sync facade
# --------------------------------------------------------------------------- #


class _LoopThread:
    """One event loop on one background thread, for the process's lifetime.

    The obvious implementation of a sync facade — `asyncio.run(coro)` per call —
    does not work here. Motor and asyncpg bind their connection pools to the
    loop that created them, so the second call would find them attached to a
    loop that no longer exists. One long-lived loop keeps every connection valid
    and makes concurrent calls from different threads safe.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="memdream-loop", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro: Any) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def run(self, coro: Any) -> Any:
        return self.submit(coro).result()

    def shutdown(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        self._loop.close()


def _sync_method(name: str):
    """Wrap one `AsyncMemDream` coroutine method as a blocking call."""

    def method(self: MemDream, *args: Any, **kwargs: Any) -> Any:
        return self._loop.run(getattr(self._async, name)(*args, **kwargs))

    async_method = getattr(AsyncMemDream, name)
    method.__name__ = name
    method.__qualname__ = f"MemDream.{name}"
    method.__doc__ = async_method.__doc__
    return method


class MemDream:
    """MemDream in-process, synchronous.

    The shortest path from nothing to working memory:

        mem = MemDream(user_id="alice")
        mem.add("I moved to Bangalore.")
        mem.search("where do they live?")

    Every method matches `DreamClient`, so moving to a deployed server later
    changes the constructor and nothing else.
    """

    #: Generated from the async facade so the two cannot drift: a method added
    #: there and not listed here simply does not exist on the sync class, which
    #: is a visible omission rather than a silent behavioural difference.
    _WRAPPED = (
        "add",
        "create",
        "update",
        "forget",
        "purge",
        "search",
        "get",
        "list",
        "history",
        "audit",
        "synthesize",
        "get_job",
        "wait_for",
        "list_jobs",
        "stats",
        "consolidate",
        "health",
    )

    def __init__(self, **kwargs: Any) -> None:
        self._async = AsyncMemDream(**kwargs)
        self._loop = _LoopThread()

        self.memory = self
        self.dream = self

    def start(self) -> MemDream:
        """Build the container now rather than on first use.

        Worth calling at startup in a server process: it moves connection setup
        and provider probing off the first user's request.
        """
        self._loop.run(self._async.start())
        return self

    @property
    def container(self) -> DreamContainer:
        """The wired container, for anything the facade does not cover."""
        return self._async.container

    def close(self) -> None:
        try:
            self._loop.run(self._async.close())
        finally:
            self._loop.shutdown()

    def __enter__(self) -> MemDream:
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def _translate(exc: Exception) -> Exception:
    """Map an engine exception onto the SDK's own exception types.

    Hosted, a failure crosses HTTP and arrives as `DreamValidationError`,
    `DreamNotFoundError` or `DreamAPIError` carrying a status code. Embedded, the
    same failure used to surface as whatever the engine raised internally — a
    pydantic `ValidationError`, an `InvalidStateTransitionError` — so an `except`
    clause written against one mode silently missed in the other.

    The status codes are the ones the API would have returned for the same
    failure. `ValueError` and `TypeError` from local argument checks pass through
    unchanged, as they do in the hosted client.
    """
    from pydantic import ValidationError as PydanticValidationError

    from app.core.exceptions import DreamError as EngineError
    from memdream.exceptions import DreamError, error_for

    if isinstance(exc, DreamError):
        return exc
    if isinstance(exc, EngineError):
        payload = {"error": {"code": exc.code, "message": exc.message, "details": exc.details}}
        return error_for(exc.status_code, payload, None)
    if isinstance(exc, PydanticValidationError):
        errors = [
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "message": error.get("msg", ""),
                "type": error.get("type", ""),
            }
            for error in exc.errors()
        ]
        payload = {
            "error": {
                "code": "validation_error",
                "message": "Request failed validation",
                "details": {"errors": errors},
            }
        }
        return error_for(422, payload, None)
    return exc


def _translating(method):
    """Wrap one `AsyncMemDream` operation so it raises SDK exception types."""

    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        try:
            return await method(self, *args, **kwargs)
        except Exception as exc:
            translated = _translate(exc)
            if translated is exc:
                raise
            raise translated from exc

    return wrapper


for _name in MemDream._WRAPPED:
    setattr(AsyncMemDream, _name, _translating(getattr(AsyncMemDream, _name)))
    setattr(MemDream, _name, _sync_method(_name))
del _name
