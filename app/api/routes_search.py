"""Search endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.deps import AuthDep, ContainerDep, ScopeDep, scope_from_request
from app.core.logging import get_logger
from app.models.requests import (
    AuditLogResponse,
    SearchMemoryRequest,
    SearchMemoryResponse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/memories", tags=["search"])


@router.post("/search", response_model=SearchMemoryResponse, summary="Search memories")
async def search_memories(
    body: SearchMemoryRequest,
    request: Request,
    container: ContainerDep,
    _auth: AuthDep = None,
) -> SearchMemoryResponse:
    """Hybrid semantic search within one user's memories.

    Ranking blends similarity with importance, confidence, recency and status —
    the weights are configurable under `retrieval.weights`, and every result
    carries its own `scoreBreakdown` so a ranking can be explained.

    Status handling:

    * default — active + superseded, so history is visible
    * `latestOnly: true` — active only, "what is true now?"
    * `includeMerged` / `includeExpired` — widen further

    Merged memories are excluded by default because their content already lives
    in the canonical memory that absorbed them.
    """
    scope = scope_from_request(body, container.settings)
    results = await container.memory_service.search(scope, body)

    return SearchMemoryResponse(
        memories=[result.public_dump() for result in results],
        count=len(results),
        request_id=getattr(request.state, "request_id", None),
    )


@router.get("/audit/operations", response_model=AuditLogResponse, summary="Dream audit log")
async def list_audit_operations(
    scope: ScopeDep,
    container: ContainerDep,
    action: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    _auth: AuthDep = None,
) -> AuditLogResponse:
    """Every Dream decision for a user, newest first.

    NOOPs are included, and are usually what you want: "why was nothing
    remembered?" is answered here rather than by reading logs.
    """
    operations = await container.audit.list_for_scope(
        scope, limit=limit, offset=offset, actions=list(action) if action else None
    )
    return AuditLogResponse(
        operations=[op.model_dump(by_alias=True, mode="json") for op in operations],
        count=len(operations),
    )
