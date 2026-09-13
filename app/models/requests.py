"""Public API request and response bodies.

These are the only camelCase-in / camelCase-out shapes callers see. Identity
arrives as a phone number (what messaging apps have) or as a caller-supplied
``userId``; either way the backend — never the caller, never the model — decides
the scope the request executes in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import DreamAction, JobStatus
from app.models.memory import _to_camel


class _APIModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class _ScopedRequest(_APIModel):
    """Shared tenancy fields. Every endpoint that touches memories uses these.

    Only the user matters. `agentId`, `clientId` and `namespace` are optional
    dimensions that further partition the memory — supply the ones your
    deployment actually has, and omit the rest. A single-agent app needs none of
    them.

    Identity arrives either as `userId` (you have your own identifier) or as
    `phoneNumber` (MemDream derives one, salted with whatever dimensions were
    supplied so it cannot collide across tenants).
    """

    user_id: str | None = Field(default=None, max_length=128)
    phone_number: str | None = Field(default=None, max_length=32)

    #: Which agent or bot. Optional.
    agent_id: str | None = Field(default=None, max_length=128)
    #: The tenant, for platforms serving several customers. Optional.
    client_id: str | None = Field(default=None, max_length=128)
    #: Any further partition dimensions: environment, region, workspace.
    namespace: dict[str, str] = Field(default_factory=dict)

    #: Previous name for `agentId`, still accepted.
    bot_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _require_identity(self) -> _ScopedRequest:
        if not self.phone_number and not self.user_id:
            raise ValueError("Either userId or phoneNumber must be provided")
        return self


# --------------------------------------------------------------------------- #
# POST /v1/memories
# --------------------------------------------------------------------------- #


class AddMemoryRequest(_ScopedRequest):
    prompt: str = Field(min_length=1, max_length=20000)
    #: Caller's id for the originating message, recorded as provenance.
    message_id: str | None = Field(default=None, max_length=128)
    #: When the message was sent, if not now. Anchors relative phrases like
    #: "last month" during extraction.
    timestamp: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: Skip extraction and store the prompt verbatim as one memory. The ADD /
    #: SUPERSEDE / MERGE decision still runs against it, so a raw write can
    #: still replace or merge with what is already known.
    raw: bool = False


class MemoryDecisionResult(_APIModel):
    """What Dream did with one extracted candidate."""

    action: DreamAction
    memory_id: str | None = None
    content: str | None = None
    reason: str = ""
    superseded_memory_ids: list[str] = Field(default_factory=list)
    merged_memory_ids: list[str] = Field(default_factory=list)
    operation_id: str | None = None


class AddMemoryResponse(_APIModel):
    """One prompt can yield several memories, so results is the real payload.

    The top-level ``action`` / ``memoryId`` / ``supersededMemoryIds`` fields
    mirror the first result, which keeps the common single-fact case as simple
    as the API contract in the spec.
    """

    action: DreamAction
    memory_id: str | None = None
    superseded_memory_ids: list[str] = Field(default_factory=list)
    merged_memory_ids: list[str] = Field(default_factory=list)
    results: list[MemoryDecisionResult] = Field(default_factory=list)
    extracted_count: int = 0
    request_id: str | None = None


# --------------------------------------------------------------------------- #
# POST /v1/memories/search
# --------------------------------------------------------------------------- #


class SearchMemoryRequest(_ScopedRequest):
    query: str = Field(min_length=1, max_length=8000)
    limit: int = Field(default=10, ge=1, le=100)
    #: Restrict to currently-true memories. Overrides the include* flags.
    latest_only: bool = False
    include_superseded: bool = True
    include_merged: bool = False
    include_expired: bool = False
    #: Filter to specific memory types (open vocabulary).
    memory_types: list[str] = Field(default_factory=list)
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    #: Point-in-time query: only memories whose validity window contains this
    #: instant. Answers "what was true on date X?".
    as_of: datetime | None = None


class SearchMemoryResponse(_APIModel):
    memories: list[dict[str, Any]] = Field(default_factory=list)
    count: int = 0
    request_id: str | None = None


# --------------------------------------------------------------------------- #
# GET /v1/memories/{memoryId}
# --------------------------------------------------------------------------- #


class RelatedMemory(_APIModel):
    memory_id: str
    relation_type: str
    content: str
    status: str
    memory_type: str


class MemoryDetailResponse(_APIModel):
    memory: dict[str, Any]
    relationships: list[RelatedMemory] = Field(default_factory=list)
    source_memories: list[RelatedMemory] = Field(default_factory=list)
    derived_memories: list[RelatedMemory] = Field(default_factory=list)
    superseded_by: str | None = None
    merged_into: str | None = None


class MemoryListResponse(_APIModel):
    memories: list[dict[str, Any]] = Field(default_factory=list)
    count: int = 0
    total: int = 0
    offset: int = 0


# --------------------------------------------------------------------------- #
# Manual memory management
#
# The Dream layer decides what to store from conversation. These endpoints are
# the operator escape hatch: importing known facts, correcting a bad extraction,
# honouring "forget that". They bypass the reasoner entirely — the caller is
# stating exactly what they want, so there is nothing to infer.
# --------------------------------------------------------------------------- #


class CreateMemoryRequest(_ScopedRequest):
    """Write a memory verbatim, with no LLM involvement."""

    content: str = Field(min_length=1, max_length=4000)
    memory_type: str = Field(default="other", max_length=64)
    importance: float = Field(default=0.6, ge=0.0, le=1.0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    #: False marks the memory as inferred rather than something the user stated.
    explicit: bool = True
    temporal_nature: str = "current"
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: By default an identical active memory is returned instead of creating a
    #: second copy. Set true to force a duplicate row.
    allow_duplicate: bool = False


class UpdateMemoryRequest(_ScopedRequest):
    """Correct an existing memory in place. Only supplied fields change.

    Restricted to `active` memories: superseded and merged rows are history, and
    history is not rewritten.
    """

    content: str | None = Field(default=None, min_length=1, max_length=4000)
    memory_type: str | None = Field(default=None, max_length=64)
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    explicit: bool | None = None
    temporal_nature: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _require_a_change(self) -> UpdateMemoryRequest:
        changed = any(
            getattr(self, name) is not None
            for name in (
                "content", "memory_type", "importance", "confidence",
                "explicit", "temporal_nature", "valid_from", "valid_until", "metadata",
            )
        )
        if not changed:
            raise ValueError("Supply at least one field to update")
        return self


class DeleteMemoryResponse(_APIModel):
    memory_id: str
    #: `expired` when retired and retained; `deleted` when erased.
    result: str
    #: True when the row was permanently removed rather than retired.
    permanent: bool = False
    operation_id: str | None = None


class PurgeResponse(_APIModel):
    """Result of erasing everything for one user."""

    deleted_memories: int = 0
    deleted_relations: int = 0
    deleted_operations: int = 0
    deleted_jobs: int = 0
    operation_id: str | None = None


# --------------------------------------------------------------------------- #
# Dream
# --------------------------------------------------------------------------- #


class SynthesizeRequest(_ScopedRequest):
    #: Run inline instead of queueing. Useful in tests and demos; never on a
    #: user-facing request path, since synthesis is measured in seconds.
    wait: bool = False
    #: Re-evaluate every cluster even if an equivalent pattern already exists.
    #: Idempotency still applies — this only widens candidate selection.
    force: bool = False
    #: Override how many active memories this user needs before synthesis will
    #: look for patterns, for this run only. Omit to use the configured default.
    #:
    #: The floor is deliberately not 1. A "pattern" drawn from one or two
    #: memories is a restatement, not a generalisation, and the whole point of
    #: the threshold is that coincidence looks like insight at small n. Lower it
    #: to see the machinery work on a small account; do not ship it that way.
    min_memories: int | None = Field(default=None, ge=2, le=1000)


class SynthesizeResponse(_APIModel):
    job_id: str
    status: JobStatus
    #: Populated only when `wait=true`.
    patterns_created: int | None = None
    memories_analyzed: int | None = None


class DreamJobResponse(_APIModel):
    job_id: str
    status: JobStatus
    patterns_created: int = 0
    patterns_updated: int = 0
    patterns_skipped: int = 0
    memories_analyzed: int = 0
    clusters_evaluated: int = 0
    created_pattern_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: float | None = None


class ConsolidateRequest(_ScopedRequest):
    """Run a consolidation pass for one user, on demand.

    The nightly scheduler does this for everybody. This endpoint exists for the
    console, for demos, and for the thing the feature actually needs before it
    can be trusted: **pointing it at real data and seeing what falls out**.

    Every threshold below overrides configuration *for this call only*. Nothing
    here changes what the nightly pass will do.
    """

    #: Defaults to **true**, unlike every other flag in this API. Consolidation
    #: retires memories, and an endpoint that does that by default on a missing
    #: field is one nobody should have to remember to disarm.
    dry_run: bool = True

    #: Also compact — fold crowds of near-identical low-value memories into one.
    #: The only part of a pass that costs a model call, hence opt-in per request
    #: as well as in configuration.
    compact: bool = False
    #: How many eligible memories a user needs before compaction is worth a
    #: model call. Lower it to see the machinery work on a small account; the
    #: configured default exists to keep a nightly pass over a thousand users
    #: affordable, so do not ship it lowered.
    compact_min_memories: int | None = Field(default=None, ge=2, le=1000)

    # --- decay overrides, for measuring a threshold before trusting it ------ #

    #: Apply the recall-decay rule in this run even when it is configured off.
    #: Decay still cannot fire without an epoch, whatever this says.
    decay: bool | None = None
    decay_after_days: int | None = Field(default=None, ge=1, le=3650)
    decay_importance_floor: float | None = Field(default=None, ge=0.0, le=1.0)
    #: When recall tracking began, ISO date. The cold-start guard — without one,
    #: every memory predating tracking reads as "never wanted".
    recall_epoch: str | None = Field(default=None, max_length=32)


class ConsolidateResponse(_APIModel):
    """What the pass did, or would have done."""

    dry_run: bool
    scanned: int = 0
    #: Validity window closed. Provably false, and retired with no model.
    lapsed: int = 0
    #: Candidates under the decay rule. Reported even when decay is off — that
    #: is how a threshold is measured before anything acts on it.
    decayed: int = 0
    #: Actually retired. Stays 0 in a dry run, and stays 0 for `decayed` while
    #: decay is off, so it never claims work it declined to do.
    expired: int = 0
    #: Left alone because an active pattern rests on them.
    protected: int = 0
    expired_ids: list[str] = Field(default_factory=list)

    #: Populated only when `compact` was requested.
    clusters: int = 0
    compacted: int = 0
    absorbed: int = 0
    rejected: int = 0

    decay_applied: bool = False
    duration_ms: float = 0.0


class AuditLogResponse(_APIModel):
    operations: list[dict[str, Any]] = Field(default_factory=list)
    count: int = 0


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


class HealthResponse(_APIModel):
    status: str
    version: str
    checks: dict[str, Any] = Field(default_factory=dict)
