"""The persistence contract.

## Why this file exists

`app/dream` and `app/memory` used to import `MemoryRepository` from the module
that also held the Mongo query documents. That worked while there was one store,
but it meant the domain layer's type annotations pointed at MongoDB — so
"support Postgres" was not a plugin you could drop in, it was a rewrite.

These abstract classes are the seam. The domain depends on them; MongoDB and
Postgres each implement them. Nothing in `app/dream` or `app/memory` imports a
concrete store, in exactly the same way nothing there imports a concrete LLM.

## The two rules every implementation must hold

1. **Scope first.** Every read and every write is filtered by `MemoryScope`.
   There is no method that touches a memory without one, so a caller cannot
   accidentally reach across tenants. Implementations re-check what they read
   before returning it — the filter should already guarantee it, and a silent
   cross-tenant read is the worst failure this system could have.

2. **No destructive deletes on the Dream path.** Status transitions are the only
   way a memory leaves the active set. The `delete_*` methods exist for
   deliberate operator action — an erasure request, a wrong extraction — and for
   tests. Nothing in `app/dream` reaches them.

## What is deliberately *not* here

Query building. Mongo composes filter documents; Postgres composes SQL with
placeholders. Trying to share a query representation between them would produce
an abstraction that fits neither, and the two implementations are each clearer
written in their own idiom. What is shared is the *contract*: same arguments,
same return values, same semantics — asserted for every backend by
`tests/test_repository_conformance.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from app.models.dream import DreamJob, DreamOperation
from app.models.enums import MemoryStatus, RelationType
from app.models.memory import Memory, MemoryRelation
from app.models.scope import MemoryScope


class Database(ABC):
    """A connection to the canonical store.

    Repositories are constructed from one of these, and the container owns its
    lifetime. Everything a memory, edge, audit row or job needs lives in here —
    the vector index is a derived artefact that can be thrown away and rebuilt.
    """

    #: Short identifier used in logs and health output ("mongodb", "postgres").
    name: str = "database"

    @abstractmethod
    async def ensure_schema(self) -> None:
        """Create collections/tables and indexes. Idempotent — runs on every boot.

        Idempotence is what lets this be a startup step rather than a migration
        tool: a fresh deployment gets its schema, an existing one gets nothing.
        """

    @abstractmethod
    async def ping(self) -> dict[str, Any]:
        """Raise `ProviderError` if the store is unreachable."""

    @abstractmethod
    async def close(self) -> None:
        """Release connections. Safe to call more than once."""

    @abstractmethod
    async def drop_everything(self) -> None:
        """Test helper. Never invoked by the service."""


class MemoryRepository(ABC):
    """Persistence for memories and the edges between them."""

    # --- writes -------------------------------------------------------------- #

    @abstractmethod
    async def insert(self, memory: Memory) -> Memory: ...

    @abstractmethod
    async def insert_many(self, memories: list[Memory]) -> list[Memory]: ...

    @abstractmethod
    async def update_fields(
        self, scope: MemoryScope, memory_id: str, fields: dict[str, Any]
    ) -> Memory | None:
        """Patch a memory in place, always bumping ``updatedAt``.

        Keys are the *wire* (camelCase) field names, and a dotted key such as
        ``metadata.supersededByOperation`` sets a nested value. Returns None when
        no row in this scope has that id.
        """

    @abstractmethod
    async def replace(self, scope: MemoryScope, memory: Memory) -> Memory: ...

    # --- lifecycle transitions ------------------------------------------------ #
    #
    # All three return the ids that *actually* reached the new status. Rows
    # already in a terminal state are skipped rather than rewritten, which is
    # what makes a replayed Dream operation idempotent instead of corrupting the
    # supersede chain.

    @abstractmethod
    async def mark_superseded(
        self,
        scope: MemoryScope,
        memory_ids: list[str],
        *,
        superseded_by: str,
        valid_until: datetime | None = None,
        operation_id: str | None = None,
    ) -> list[str]: ...

    @abstractmethod
    async def mark_merged(
        self,
        scope: MemoryScope,
        memory_ids: list[str],
        *,
        merged_into: str,
        operation_id: str | None = None,
    ) -> list[str]:
        """Raises `InvalidStateTransitionError` if a memory is merged into itself."""

    @abstractmethod
    async def mark_expired(
        self, scope: MemoryScope, memory_ids: list[str], *, valid_until: datetime | None = None
    ) -> list[str]: ...

    @abstractmethod
    async def record_recall(
        self, scope: MemoryScope, memory_ids: list[str], *, at: datetime | None = None
    ) -> int:
        """Note that these memories were returned by a search.

        Sets `lastRecalledAt` and increments `recallCount`. **Must not touch
        `updatedAt`** — a read is not a write, and letting it count as one would
        feed the ranker's recency term back into itself.

        Best-effort by contract: callers treat a failure here as nothing
        happened, because losing a recall count must never fail a search.
        """

    @abstractmethod
    async def mark_stale(
        self, scope: MemoryScope, memory_ids: list[str], stale: bool = True
    ) -> int:
        """Flag patterns for re-evaluation. Never deletes them."""

    # --- reads ---------------------------------------------------------------- #

    @abstractmethod
    async def get(self, scope: MemoryScope, memory_id: str) -> Memory | None: ...

    @abstractmethod
    async def get_many(self, scope: MemoryScope, memory_ids: list[str]) -> list[Memory]:
        """Fetch by id, **preserving the caller's ordering**.

        Load-bearing: callers pass ids in relevance order from the vector index
        and expect that order back. Ids not found are omitted, not padded.
        """

    @abstractmethod
    async def find(
        self,
        scope: MemoryScope,
        *,
        statuses: list[MemoryStatus] | tuple[MemoryStatus, ...] | None = None,
        memory_types: list[str] | None = None,
        exclude_types: list[str] | None = None,
        as_of: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        sort_field: str = "updatedAt",
        include_embedding: bool = True,
    ) -> list[Memory]:
        """Filtered browse, newest first by ``sort_field``.

        ``as_of`` selects memories whose validity window contains that instant.
        An absent bound means "unknown", not "excluded": no ``validFrom`` reads
        as always-having-been-true, no ``validUntil`` as still true.
        """

    @abstractmethod
    async def eligible_ids(
        self,
        scope: MemoryScope,
        *,
        statuses: list[MemoryStatus] | tuple[MemoryStatus, ...] | None = None,
        memory_types: list[str] | None = None,
        exclude_types: list[str] | None = None,
        as_of: datetime | None = None,
    ) -> set[str]:
        """The id set the vector index is allowed to return.

        This is the pre-filter that makes retrieval correct: metadata narrowing
        happens here, *then* similarity ranks what survived. The other order
        would let a superseded or out-of-window memory displace a valid one from
        the top-k.
        """

    @abstractmethod
    async def count(
        self,
        scope: MemoryScope,
        statuses: list[MemoryStatus] | tuple[MemoryStatus, ...] | None = None,
        memory_types: list[str] | None = None,
        exclude_types: list[str] | None = None,
    ) -> int: ...

    @abstractmethod
    async def count_since(self, scope: MemoryScope, since: datetime | None = None) -> int:
        """Active non-pattern memories created after ``since``.

        The auto-trigger's signal. ``since=None`` means never synthesised, so
        everything counts as new.
        """

    @abstractmethod
    async def find_by_content_hash(
        self,
        scope: MemoryScope,
        content_hash: str,
        statuses: tuple[MemoryStatus, ...] = (MemoryStatus.ACTIVE,),
    ) -> Memory | None:
        """Exact-duplicate lookup — deterministic, no LLM, no embedding."""

    @abstractmethod
    async def find_patterns(
        self, scope: MemoryScope, *, only_active: bool = True, limit: int = 500
    ) -> list[Memory]: ...

    @abstractmethod
    async def find_patterns_referencing(
        self, scope: MemoryScope, source_memory_ids: list[str]
    ) -> list[Memory]:
        """Patterns whose evidence includes any of these memories.

        Called after a supersede or merge, so patterns resting on the changed
        memory get re-evaluated on the next synthesis run.
        """

    @abstractmethod
    async def iter_all(self, scope: MemoryScope, batch_size: int = 500) -> list[Memory]:
        """Every memory in a scope, embeddings included. For index rebuilds."""

    @abstractmethod
    async def distinct_scopes(self, limit: int = 10000) -> list[dict[str, str]]:
        """Every partition that has memories, with a row count.

        Drives scheduled synthesis, which has to walk users without being told
        who they are.
        """

    # --- relations -------------------------------------------------------------- #

    @abstractmethod
    async def add_relation(
        self,
        scope: MemoryScope,
        from_memory_id: str,
        to_memory_id: str,
        relation_type: RelationType,
        *,
        operation_id: str | None = None,
        bidirectional: bool = True,
    ) -> None:
        """Record an edge, and by default its inverse.

        Writing the same edge twice is a no-op, not an error: re-running a Dream
        operation must converge on the same graph rather than fail. Self-edges
        are silently ignored.
        """

    @abstractmethod
    async def add_relations(
        self,
        scope: MemoryScope,
        from_memory_id: str,
        to_memory_ids: list[str],
        relation_type: RelationType,
        *,
        operation_id: str | None = None,
    ) -> None: ...

    @abstractmethod
    async def get_relations(
        self,
        scope: MemoryScope,
        memory_id: str,
        relation_types: list[RelationType] | None = None,
    ) -> list[MemoryRelation]:
        """Outgoing edges from a memory."""

    @abstractmethod
    async def get_incoming_relations(
        self,
        scope: MemoryScope,
        memory_id: str,
        relation_types: list[RelationType] | None = None,
    ) -> list[MemoryRelation]: ...

    # --- destructive ------------------------------------------------------------- #

    @abstractmethod
    async def delete_memories(self, scope: MemoryScope, memory_ids: list[str]) -> int:
        """Permanently remove memories and every edge touching them.

        Inbound pointers (`supersededBy`, `mergedInto`, `sourceMemoryIds`) are
        cleared too: a memory pointing at something that no longer exists is
        worse than one pointing at nothing.
        """

    @abstractmethod
    async def delete_scope(self, scope: MemoryScope) -> dict[str, int]:
        """Hard-delete everything for one user, audit trail included.

        This is the right-to-erasure path. Retaining "we decided to store X about
        this user" after deleting X would defeat the point of the request.
        """


class AuditRepository(ABC):
    """Persistence for the Dream audit log.

    Every decision writes a row here, NOOPs included — those are the ones you
    want when a threshold is misconfigured and nothing is being remembered.
    """

    @abstractmethod
    async def record(self, operation: DreamOperation) -> DreamOperation:
        """Write one audit row.

        **Must never raise.** The memory is already committed by the time this is
        called, so a logging failure has to be logged and swallowed rather than
        surfacing to the caller as a false error.
        """

    @abstractmethod
    async def get(self, operation_id: str) -> DreamOperation | None: ...

    @abstractmethod
    async def list_for_scope(
        self,
        scope: MemoryScope,
        *,
        limit: int = 50,
        offset: int = 0,
        actions: list[str] | None = None,
        since: datetime | None = None,
    ) -> list[DreamOperation]: ...

    @abstractmethod
    async def list_for_memory(self, memory_id: str, limit: int = 50) -> list[DreamOperation]:
        """The decision history of one memory, newest first."""

    @abstractmethod
    async def list_for_job(self, job_id: str, limit: int = 500) -> list[DreamOperation]: ...

    @abstractmethod
    async def token_usage(self, scope: MemoryScope) -> dict[str, Any]:
        """Tokens spent on one user, summed from their audit rows.

        Durable and per-user, unlike the in-process metrics registry: it survives
        restarts and is correct across replicas, because every decision records
        what it cost on the row it wrote.

        Shape: ``{"byAction": {action: {...}}, "totals": {...}}``.
        """

    @abstractmethod
    async def action_counts(self, scope: MemoryScope) -> dict[str, int]:
        """How often each action fired — the first thing to look at when tuning
        merge and supersede thresholds."""


class JobRepository(ABC):
    """Persistence for Dream jobs.

    The job row is the single source of truth for a synthesis run's status. The
    queue only carries a job id, so the API can answer
    ``GET /v1/dream/jobs/{id}`` without talking to the broker.
    """

    @abstractmethod
    async def create(self, job: DreamJob) -> DreamJob: ...

    @abstractmethod
    async def get(self, job_id: str) -> DreamJob | None: ...

    @abstractmethod
    async def mark_running(self, job_id: str) -> None: ...

    @abstractmethod
    async def complete(self, job: DreamJob) -> DreamJob:
        """Write the terminal state, computing duration from the row's own
        timestamps so a worker restart cannot produce a negative elapsed time."""

    @abstractmethod
    async def fail(self, job_id: str, error: str) -> None: ...

    @abstractmethod
    async def list_for_scope(
        self, scope: MemoryScope, *, limit: int = 20, offset: int = 0
    ) -> list[DreamJob]: ...

    @abstractmethod
    async def find_active_for_scope(
        self, scope: MemoryScope, *, stale_after_seconds: int = 900
    ) -> DreamJob | None:
        """A *genuinely* in-flight job for this user, if any.

        The staleness cut-off is what makes deduplication safe. A worker that
        dies mid-synthesis leaves its row at `running` forever, and without an
        age check that row would block this user's synthesis permanently — the
        guard silently becoming a denial of service.
        """

    @abstractmethod
    async def reap_stale(self, *, stale_after_seconds: int = 900) -> int:
        """Mark abandoned jobs failed so they stop looking in-flight.

        Housekeeping, not correctness: `find_active_for_scope` already ignores
        them. This exists so operators see `failed` with a reason rather than
        rows stuck at `running`.
        """

    @abstractmethod
    async def claim_next(self, worker_id: str) -> DreamJob | None:
        """Atomically claim one queued job, or None.

        Must be a single atomic operation: two workers polling simultaneously
        cannot be allowed to claim the same job. This is what lets the canonical
        store act as the queue without a broker.
        """

    @abstractmethod
    async def last_completed_at(self, scope: MemoryScope) -> datetime | None:
        """When this user's most recent synthesis finished, or None."""

    @abstractmethod
    async def running_count(self) -> int:
        """Jobs in flight across all users, for the concurrency cap."""

    @abstractmethod
    async def stats(self) -> dict[str, Any]: ...
