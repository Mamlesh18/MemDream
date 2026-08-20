"""Memory endpoints: add, manual create/edit, get, list, history, audit, delete."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request

from app.api.deps import AuthDep, ContainerDep, ScopeDep, scope_from_request
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.enums import DreamAction, MemoryStatus
from app.models.requests import (
    AddMemoryRequest,
    AddMemoryResponse,
    AuditLogResponse,
    CreateMemoryRequest,
    DeleteMemoryResponse,
    MemoryDecisionResult,
    MemoryDetailResponse,
    MemoryListResponse,
    PurgeResponse,
    UpdateMemoryRequest,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/memories", tags=["memories"])


@router.post("", response_model=AddMemoryResponse, summary="Remember a message")
async def add_memory(
    body: AddMemoryRequest,
    request: Request,
    container: ContainerDep,
    _auth: AuthDep = None,
) -> AddMemoryResponse:
    """Extract memories from a message and let Dream decide what to do with them.

    One message can hold several facts, so `results` is the complete answer. The
    flat `action` / `memoryId` fields mirror the most significant result — a
    consolidation outranks an addition — which keeps the single-fact case, by far
    the most common, as simple as it looks.
    """
    scope = scope_from_request(body, container.settings)
    request_id = getattr(request.state, "request_id", None)

    outcome = await container.memory_service.add(
        scope,
        body.prompt,
        message_id=body.message_id,
        timestamp=body.timestamp,
        metadata=body.metadata,
        raw=body.raw,
        request_id=request_id,
    )

    results = [
        MemoryDecisionResult(
            action=result.action,
            memory_id=result.memory_id,
            content=result.memory.content if result.memory else None,
            reason=result.reason,
            superseded_memory_ids=result.superseded_memory_ids,
            merged_memory_ids=result.merged_memory_ids,
            operation_id=result.operation_id,
        )
        for result in outcome.results
    ]

    # Synthesis fires on accumulated material rather than a timer. Deliberately
    # after the response is assembled and never allowed to raise — a background
    # nicety must not fail the user's write.
    await container.dream_service.maybe_trigger(scope)

    primary = outcome.primary
    return AddMemoryResponse(
        action=primary.action if primary else DreamAction.NOOP,
        memory_id=primary.memory_id if primary else None,
        superseded_memory_ids=primary.superseded_memory_ids if primary else [],
        merged_memory_ids=primary.merged_memory_ids if primary else [],
        results=results,
        extracted_count=outcome.extracted_count,
        request_id=request_id,
    )


@router.post(
    "/manual",
    response_model=MemoryDetailResponse,
    status_code=201,
    summary="Create a memory manually",
)
async def create_memory_manually(
    body: CreateMemoryRequest,
    request: Request,
    container: ContainerDep,
    _auth: AuthDep = None,
) -> MemoryDetailResponse:
    """Write a memory verbatim, bypassing extraction and Dream reasoning.

    `POST /v1/memories` is the conversational path: it decides *what* is worth
    remembering. This is the deliberate path — importing known facts, or storing
    something the extraction policy would have rejected. Nothing is inferred,
    because you have already said exactly what you want.

    Write content in the third person (`"User is allergic to peanuts."`), matching
    what the extractor produces, so retrieval treats it the same way.

    An identical active memory is returned as-is rather than duplicated; pass
    `allowDuplicate: true` to force a second row.
    """
    scope = scope_from_request(body, container.settings)

    memory, created = await container.memory_service.create_manual(
        scope,
        content=body.content,
        memory_type=body.memory_type,
        importance=body.importance,
        confidence=body.confidence,
        explicit=body.explicit,
        temporal_nature=body.temporal_nature,
        valid_from=body.valid_from,
        valid_until=body.valid_until,
        metadata=body.metadata,
        allow_duplicate=body.allow_duplicate,
        request_id=getattr(request.state, "request_id", None),
    )

    logger.info("memory_created_manually", memory_id=memory.memory_id, created=created)
    return MemoryDetailResponse(
        memory=memory.public_dump(),
        superseded_by=memory.superseded_by,
        merged_into=memory.merged_into,
    )


@router.patch(
    "/{memory_id}", response_model=MemoryDetailResponse, summary="Edit a memory"
)
async def update_memory(
    body: UpdateMemoryRequest,
    request: Request,
    container: ContainerDep,
    memory_id: Annotated[str, Path()],
    _auth: AuthDep = None,
) -> MemoryDetailResponse:
    """Correct an active memory in place. Only the fields you send change.

    The memory keeps its id, so anything referencing it stays valid. Editing is
    deliberately *not* a supersede: an operator fixing a bad extraction is
    correcting a record, whereas a supersede chain would claim the user's
    situation changed — which would put a falsehood into the history.

    Only `active` memories can be edited. Superseded and merged rows are history,
    and history is not rewritten (`409`).
    """
    scope = scope_from_request(body, container.settings)

    memory = await container.memory_service.update_manual(
        scope,
        memory_id,
        changes=body.model_dump(
            exclude={"client_id", "bot_id", "phone_number", "user_id"},
            exclude_unset=True,
        ),
        request_id=getattr(request.state, "request_id", None),
    )
    if memory is None:
        raise NotFoundError(f"Memory {memory_id} not found")

    return MemoryDetailResponse(
        memory=memory.public_dump(),
        superseded_by=memory.superseded_by,
        merged_into=memory.merged_into,
    )


@router.delete(
    "/{memory_id}", response_model=DeleteMemoryResponse, summary="Forget one memory"
)
async def delete_memory(
    scope: ScopeDep,
    request: Request,
    container: ContainerDep,
    memory_id: Annotated[str, Path()],
    permanent: Annotated[bool, Query()] = False,
    _auth: AuthDep = None,
) -> DeleteMemoryResponse:
    """Retire a memory, or erase it.

    Default (`permanent=false`) is the reversible one: the memory moves to
    `expired` and leaves the search index, so it stops influencing answers but
    stays auditable. This is what "forget that" should do.

    `permanent=true` erases the row and every edge touching it. Irreversible.
    Reserve it for data that must not exist — a wrongly captured secret, or an
    erasure request.

    Two verbs rather than one because "stop using this" and "destroy this" carry
    very different risk, and merging them is how data gets lost by accident.
    """
    memory, found = await container.memory_service.forget_memory(
        scope,
        memory_id,
        permanent=permanent,
        request_id=getattr(request.state, "request_id", None),
    )
    if not found:
        raise NotFoundError(f"Memory {memory_id} not found")

    logger.info("memory_forgotten_via_api", memory_id=memory_id, permanent=permanent)
    return DeleteMemoryResponse(
        memory_id=memory_id,
        result="deleted" if permanent else "expired",
        permanent=permanent,
    )


@router.delete("", response_model=PurgeResponse, summary="Erase all of a user's memory")
async def purge_user_memories(
    scope: ScopeDep,
    container: ContainerDep,
    confirm: Annotated[str, Query()] = "",
    _auth: AuthDep = None,
) -> PurgeResponse:
    """Erase everything for one user: memories, relationships, audit trail, jobs.

    Irreversible, and the audit trail goes too — retaining "we decided to store X
    about this user" after deleting X would defeat an erasure request.

    Requires `?confirm=ERASE`. A destructive collection-level operation should
    not be reachable by getting a URL slightly wrong.
    """
    if confirm != "ERASE":
        raise ValidationError(
            "Erasing a user's entire memory requires ?confirm=ERASE",
            details={"userId": scope.user_id},
        )

    counts = await container.memory_service.purge_user(scope)
    logger.warning("user_memory_purged", user_id=scope.user_id, **counts)

    return PurgeResponse(
        deleted_memories=counts.get("memories", 0),
        deleted_relations=counts.get("relations", 0),
        deleted_operations=counts.get("operations", 0),
        deleted_jobs=counts.get("jobs", 0),
    )


@router.get("", response_model=MemoryListResponse, summary="List memories")
async def list_memories(
    scope: ScopeDep,
    container: ContainerDep,
    status: Annotated[list[MemoryStatus] | None, Query()] = None,
    memory_type: Annotated[list[str] | None, Query(alias="memoryType")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    _auth: AuthDep = None,
) -> MemoryListResponse:
    """Browse a user's memories without a semantic query.

    Defaults to active memories only; pass `status` explicitly to include
    superseded or merged history.
    """
    statuses = list(status) if status else [MemoryStatus.ACTIVE]
    memories, total = await container.memory_service.list_memories(
        scope,
        statuses=statuses,
        memory_types=list(memory_type) if memory_type else None,
        limit=limit,
        offset=offset,
    )
    return MemoryListResponse(
        memories=[memory.public_dump() for memory in memories],
        count=len(memories),
        total=total,
        offset=offset,
    )


@router.get(
    "/{memory_id}", response_model=MemoryDetailResponse, summary="Get one memory"
)
async def get_memory(
    scope: ScopeDep,
    container: ContainerDep,
    memory_id: Annotated[str, Path(alias="memory_id")],
    _auth: AuthDep = None,
) -> MemoryDetailResponse:
    """One memory with its relationships, sources and whatever derives from it."""
    detail = await container.memory_service.get_detail(scope, memory_id)
    if detail is None:
        raise NotFoundError(f"Memory {memory_id} not found")

    return MemoryDetailResponse(
        memory=detail.memory.public_dump(),
        relationships=detail.relationships,
        source_memories=detail.source_memories,
        derived_memories=detail.derived_memories,
        superseded_by=detail.memory.superseded_by,
        merged_into=detail.memory.merged_into,
    )


@router.get(
    "/{memory_id}/history",
    response_model=MemoryListResponse,
    summary="Walk a memory's supersede chain",
)
async def get_memory_history(
    scope: ScopeDep,
    container: ContainerDep,
    memory_id: Annotated[str, Path(alias="memory_id")],
    _auth: AuthDep = None,
) -> MemoryListResponse:
    """Follow `supersedes` edges back to the original fact.

    Answers "when did this change, and what did it say before?" — the reason
    superseded memories are retained rather than deleted.
    """
    chain = await container.memory_service.history(scope, memory_id)
    if not chain:
        raise NotFoundError(f"Memory {memory_id} not found")
    return MemoryListResponse(
        memories=[memory.public_dump() for memory in chain],
        count=len(chain),
        total=len(chain),
    )


@router.get(
    "/{memory_id}/audit", response_model=AuditLogResponse, summary="Audit trail for a memory"
)
async def get_memory_audit(
    scope: ScopeDep,
    container: ContainerDep,
    memory_id: Annotated[str, Path(alias="memory_id")],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    _auth: AuthDep = None,
) -> AuditLogResponse:
    """Every Dream operation that touched this memory."""
    # Scope-check the memory first: the audit collection is keyed by memory id,
    # so reading it directly would otherwise bypass the tenancy filter.
    memory = await container.memories.get(scope, memory_id)
    if memory is None:
        raise NotFoundError(f"Memory {memory_id} not found")

    operations = await container.audit.list_for_memory(memory_id, limit=limit)
    return AuditLogResponse(
        operations=[op.model_dump(by_alias=True, mode="json") for op in operations],
        count=len(operations),
    )
