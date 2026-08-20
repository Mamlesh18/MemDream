"""Dream endpoints: trigger synthesis, poll jobs, inspect stats."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import AuthDep, ContainerDep, ScopeDep, scope_from_request
from app.core.logging import get_logger
from app.models.requests import DreamJobResponse, SynthesizeRequest, SynthesizeResponse

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/dream", tags=["dream"])


@router.post("/synthesize", response_model=SynthesizeResponse, summary="Trigger synthesis")
async def trigger_synthesis(
    body: SynthesizeRequest,
    container: ContainerDep,
    principal: AuthDep,
) -> SynthesizeResponse:
    """Queue a background synthesis run for one user.

    Returns immediately with a job id; poll `GET /v1/dream/jobs/{jobId}` for the
    outcome. If a run is already queued or in flight for this user, that job is
    returned instead of a second one being created.

    `wait: true` runs synthesis inline and returns the finished job. It exists
    for tests, demos and CLI use — synthesis takes seconds, so it has no place on
    a user-facing request path.
    """
    scope = scope_from_request(body, principal, container.settings)
    job = await container.dream_service.request_synthesis(
        scope, wait=body.wait, force=body.force
    )

    return SynthesizeResponse(
        job_id=job.job_id,
        status=job.status,
        patterns_created=job.patterns_created if body.wait else None,
        memories_analyzed=job.memories_analyzed if body.wait else None,
    )


@router.get(
    "/jobs/{job_id}", response_model=DreamJobResponse, summary="Dream job status"
)
async def get_dream_job(
    scope: ScopeDep,
    container: ContainerDep,
    job_id: Annotated[str, Path(alias="job_id")],
) -> DreamJobResponse:
    """Status and results of one synthesis job.

    Scope query parameters are required. A job id is a random handle, not an
    authorisation token, and job records disclose how much of a user is
    remembered — so a job belonging to another tenant reports as not found.
    """
    job = await container.dream_service.get_job(job_id, scope)
    return DreamJobResponse(
        job_id=job.job_id,
        status=job.status,
        patterns_created=job.patterns_created,
        patterns_updated=job.patterns_updated,
        patterns_skipped=job.patterns_skipped,
        memories_analyzed=job.memories_analyzed,
        clusters_evaluated=job.clusters_evaluated,
        created_pattern_ids=job.created_pattern_ids,
        error=job.error,
        created_at=job.created_at,
        completed_at=job.completed_at,
        duration_ms=job.duration_ms,
    )


@router.get("/jobs", summary="List Dream jobs")
async def list_dream_jobs(
    scope: ScopeDep,
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    jobs = await container.dream_service.list_jobs(scope, limit=limit, offset=offset)
    return {"jobs": [job.public_dump() for job in jobs], "count": len(jobs)}


@router.get("/stats", summary="Memory statistics for a user")
async def dream_stats(
    scope: ScopeDep,
    container: ContainerDep,
) -> dict:
    """Memory counts by status, pattern count, and Dream action tallies.

    The action tallies are the first place to look when tuning thresholds: all
    NOOPs means the bars are too high, all ADDs means consolidation is not firing.
    """
    return await container.memory_service.stats(scope)
