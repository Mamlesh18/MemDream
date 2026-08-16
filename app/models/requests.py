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
    """Shared tenancy fields. Every endpoint that touches memories uses these."""

    client_id: str = Field(min_length=1, max_length=128)
    bot_id: str = Field(min_length=1, max_length=128)
    phone_number: str | None = Field(default=None, max_length=32)
    #: Supply this instead of a phone number when the caller already has its own
    #: stable user identifier.
    user_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _require_identity(self) -> _ScopedRequest:
        if not self.phone_number and not self.user_id:
            raise ValueError("Either phoneNumber or userId must be provided")
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
    #: Skip the Dream reasoner and store the prompt verbatim as one memory.
    #: For callers that have already decided what to remember.
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
