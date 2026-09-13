"""Dream endpoints: trigger synthesis, poll jobs, inspect stats."""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import AuthDep, ContainerDep, ScopeDep, scope_from_request
from app.core.logging import get_logger
from app.models.requests import (
    ConsolidateRequest,
    ConsolidateResponse,
    DreamJobResponse,
    SynthesizeRequest,
    SynthesizeResponse,
)

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
        scope, wait=body.wait, force=body.force, min_memories=body.min_memories
    )

    return SynthesizeResponse(
        job_id=job.job_id,
        status=job.status,
        patterns_created=job.patterns_created if body.wait else None,
        memories_analyzed=job.memories_analyzed if body.wait else None,
    )


@router.post(
    "/consolidate", response_model=ConsolidateResponse, summary="Run consolidation"
)
async def run_consolidation(
    body: ConsolidateRequest,
    container: ContainerDep,
    principal: AuthDep,
) -> ConsolidateResponse:
    """Retire memories that no longer earn their place, for one user.

    The nightly scheduler already does this for everybody. This runs it now, for
    one user, and — by default — **reports without changing anything**.

    Two rules, and the difference between them is the whole design:

    * **lapsed** — the validity window has closed, so the memory is not stale,
      it is *false*. Deterministic, no model, no embedding, always applied.
    * **decayed** — nothing has recalled it inside the window. That is a
      judgement rather than a fact, so it is off unless asked for, and needs an
      epoch before it can fire at all.

    Nothing is deleted either way. Retiring means a status change to `expired`:
    the row is kept and leaves the search index, so a wrong call costs a flip
    rather than a row.

    `dryRun` defaults to **true**. Overrides in the body apply to this call
    only — they never change what the nightly pass will do, which matters
    because one process serves every tenant.
    """
    scope = scope_from_request(body, principal, container.settings)
    started = time.perf_counter()

    # A copy, never the live settings object: mutating that would change the
    # rules for every other tenant's pass in this process.
    overrides: dict[str, object] = {}
    if body.decay is not None:
        overrides["decay_enabled"] = body.decay
    if body.decay_after_days is not None:
        overrides["decay_after_days"] = body.decay_after_days
    if body.decay_importance_floor is not None:
        overrides["decay_importance_floor"] = body.decay_importance_floor
    if body.recall_epoch is not None:
        overrides["recall_epoch"] = body.recall_epoch
    config = container.settings.dream.consolidation.model_copy(update=overrides)

    result = await container.consolidation.run(scope, dry_run=body.dry_run, config=config)

    response = ConsolidateResponse(
        dry_run=body.dry_run,
        scanned=result.scanned,
        lapsed=len(result.lapsed),
        decayed=len(result.decayed),
        expired=result.expired_count,
        protected=len(result.protected),
        expired_ids=result.expired[:20],
        decay_applied=config.decay_enabled and bool(config.recall_epoch),
    )

    if body.compact:
        # Retire what is dead before compressing what is left — no point paying
        # a model call to summarise memories that were about to be expired.
        compaction_config = container.settings.dream.compaction
        if body.compact_min_memories is not None:
            compaction_config = compaction_config.model_copy(
                update={"min_memories": body.compact_min_memories}
            )
        folded = await container.compaction.run(
            scope, dry_run=body.dry_run, config=compaction_config
        )
        response.clusters = folded.clusters
        response.compacted = len(folded.compacted)
        response.absorbed = folded.absorbed
        response.rejected = folded.rejected

    response.duration_ms = (time.perf_counter() - started) * 1000.0
    logger.info(
        "consolidate_requested",
        dry_run=body.dry_run,
        scanned=response.scanned,
        expired=response.expired,
        compact=body.compact,
    )
    return response


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
