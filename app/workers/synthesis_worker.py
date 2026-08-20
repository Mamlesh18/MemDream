"""The background synthesis worker.

Run with:

    arq app.workers.synthesis_worker.WorkerSettings

Builds the same `DreamContainer` the API does, so worker and API share one
wiring path and one set of provider implementations.

Two ways work arrives:

* **On demand** — `POST /v1/dream/synthesize` enqueues `run_synthesis_task`.
* **Scheduled** — an optional cron job walks every user with memories and
  synthesises each in turn.
"""

from __future__ import annotations

from typing import Any

from app.container import DreamContainer
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger, operation_context
from app.core.metrics import metrics
from app.models.scope import MemoryScope

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #


async def run_synthesis_task(
    ctx: dict[str, Any],
    job_id: str,
    client_id: str,
    bot_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Run one synthesis job.

    Named to match `app.providers.queue.arq_queue.SYNTHESIS_TASK`.
    """
    container: DreamContainer = ctx["container"]
    scope = MemoryScope(client_id=client_id, bot_id=bot_id, user_id=user_id)

    with operation_context():
        logger.info("synthesis_task_started", job_id=job_id, user_id=user_id)
        job = await container.dream_service.run_job(job_id, scope)

    return {
        "jobId": job.job_id,
        "status": job.status.value,
        "patternsCreated": job.patterns_created,
        "patternsUpdated": job.patterns_updated,
        "memoriesAnalyzed": job.memories_analyzed,
    }


async def scheduled_synthesis(ctx: dict[str, Any]) -> dict[str, Any]:
    """Walk every user with memories and synthesise each.

    Users are processed sequentially. Synthesis is a background nicety, and a
    burst of parallel LLM calls would be the thing that rate-limits the request
    path that users are actually waiting on.
    """
    container: DreamContainer = ctx["container"]
    scopes = await container.memories.distinct_scopes()

    logger.info("scheduled_synthesis_started", users=len(scopes))
    processed = 0
    patterns_created = 0
    failed = 0

    for entry in scopes:
        # Every dimension, not just the triple: a scope missing one computes a
        # scopeKey that matches none of this user's memories.
        scope = MemoryScope(
            user_id=entry["userId"],
            agent_id=entry.get("agentId"),
            client_id=entry.get("clientId"),
            namespace=entry.get("namespace") or {},
        )
        try:
            job = await container.dream_service.request_synthesis(
                scope, wait=True, requested_by="scheduler"
            )
            patterns_created += job.patterns_created
            processed += 1
        except Exception as exc:  # noqa: BLE001 - one user must not stop the sweep
            failed += 1
            logger.error(
                "scheduled_synthesis_user_failed",
                user_id=entry["userId"],
                error=str(exc)[:300],
            )

    logger.info(
        "scheduled_synthesis_finished",
        processed=processed,
        failed=failed,
        patterns_created=patterns_created,
    )
    return {"processed": processed, "failed": failed, "patternsCreated": patterns_created}


# --------------------------------------------------------------------------- #
# Worker lifecycle
# --------------------------------------------------------------------------- #


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    if settings.otel_enabled:
        metrics.enable_otel(settings.service_name)
    ctx["container"] = await DreamContainer.create(settings)
    logger.info("synthesis_worker_ready")


async def shutdown(ctx: dict[str, Any]) -> None:
    container: DreamContainer | None = ctx.get("container")
    if container is not None:
        await container.close()
    logger.info("synthesis_worker_stopped")


def _redis_settings() -> Any:
    from arq.connections import RedisSettings

    return RedisSettings.from_dsn(get_settings().redis_url)


def _build_cron_jobs() -> list:
    """Register the scheduled sweep when it is enabled."""
    settings = get_settings()
    if not settings.dream.synthesis.schedule_enabled:
        return []

    from arq import cron

    minute, hour, day, month, weekday = (
        settings.dream.synthesis.schedule_cron.split() + ["*"] * 5
    )[:5]

    def _field(value: str) -> Any:
        # arq wants None for "any" and an int for a fixed value. Ranges and step
        # syntax are not supported here; use the queue's own scheduler for those.
        return None if value == "*" else int(value)

    return [
        cron(
            scheduled_synthesis,
            minute=_field(minute),
            hour=_field(hour),
            day=_field(day),
            month=_field(month),
            weekday=_field(weekday),
            run_at_startup=False,
        )
    ]


class WorkerSettings:
    """ARQ worker configuration."""

    functions = [run_synthesis_task]
    cron_jobs = _build_cron_jobs()

    on_startup = startup
    on_shutdown = shutdown

    # Synthesis is LLM-bound, so a handful of concurrent jobs saturates the
    # deployment's rate limit long before it saturates the worker.
    max_jobs = 4
    job_timeout = 900
    keep_result = 3600
    max_tries = 2

    redis_settings = _redis_settings()
