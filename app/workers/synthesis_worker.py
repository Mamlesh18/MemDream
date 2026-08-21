"""The optional synthesis worker.

**You probably do not need this file.** By default synthesis runs inside the API
process (`DREAM_QUEUE_PROVIDER=inline`), and the whole deployment is:

    uvicorn app.main:app

This worker exists for the one case that cannot answer: several API replicas
sharing a single queue, where a job must be claimed exactly once and must
survive the process that queued it restarting. Then:

    DREAM_QUEUE_PROVIDER=database
    uvicorn app.main:app                        # terminal 1, or N replicas
    python -m app.workers.synthesis_worker      # terminal 2

Still no broker. The `dream_jobs` table *is* the queue, and this process claims
rows from it with an atomic compare-and-swap — `find_one_and_update` on MongoDB,
`FOR UPDATE SKIP LOCKED` on Postgres. Two workers polling at the same instant
cannot claim the same job either way.

It builds the same `DreamContainer` the API does, so there is one wiring path and
one set of provider implementations, not two that can drift.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from app.container import DreamContainer
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger, operation_context
from app.core.metrics import metrics
from app.models.scope import MemoryScope

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #


async def synthesise_everyone(container: DreamContainer) -> dict[str, Any]:
    """Walk every user with memories and synthesise each in turn.

    Rarely needed: synthesis fires automatically once a user accumulates enough
    new material, which covers the case a nightly sweep was invented for. This
    is here for a backfill after changing the synthesis prompt or thresholds,
    where you want every user re-evaluated at once.

    Users are processed **sequentially**. Synthesis is a background nicety, and
    a burst of parallel LLM calls would be the thing that rate-limits the
    request path users are actually waiting on.
    """
    scopes = await container.memories.distinct_scopes()

    logger.info("sweep_started", users=len(scopes))
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
                scope, wait=True, requested_by="sweep"
            )
            patterns_created += job.patterns_created
            processed += 1
        except Exception as exc:  # noqa: BLE001 - one user must not stop the sweep
            failed += 1
            logger.error(
                "sweep_user_failed", user_id=entry["userId"], error=str(exc)[:300]
            )

    logger.info(
        "sweep_finished",
        processed=processed,
        failed=failed,
        patterns_created=patterns_created,
    )
    return {"processed": processed, "failed": failed, "patternsCreated": patterns_created}


# --------------------------------------------------------------------------- #
# The worker loop
# --------------------------------------------------------------------------- #


async def run_forever() -> None:
    """Poll the canonical store for queued jobs and run them.

    The counterpart to `DatabaseJobQueue`: that class writes and claims, this is
    the process doing the claiming. Everything about *how* a job is claimed
    lives in the repository, so this stays a loop and a handler.

    Stops cleanly on SIGINT/SIGTERM, letting a job already in flight finish —
    abandoning it would leave a row at `running` until the stale timeout, and
    nothing could retry it in the meantime.
    """
    from app.providers.queue.database_queue import DatabaseJobQueue

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    if settings.otel_enabled:
        metrics.enable_otel(settings.service_name)

    container = await DreamContainer.create(settings)
    queue = container.queue

    if not isinstance(queue, DatabaseJobQueue):
        # Refuse rather than start a loop that will never see a job. Silently
        # idling here is how you end up debugging the API instead.
        await container.close()
        raise SystemExit(
            f"This worker claims jobs from the canonical store, but "
            f"DREAM_QUEUE_PROVIDER is {settings.queue_provider!r}.\n"
            "  inline    -> no worker needed. Synthesis runs in the API process,\n"
            "               which is the default and almost certainly what you want.\n"
            "  database  -> python -m app.workers.synthesis_worker  (this)"
        )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGINT", "SIGTERM"):
        signal_number = getattr(signal, signal_name, None)
        if signal_number is None:
            continue
        with contextlib.suppress(NotImplementedError):  # Windows lacks add_signal_handler
            loop.add_signal_handler(signal_number, stop.set)

    async def handle(job) -> None:
        with operation_context():
            logger.info("synthesis_task_started", job_id=job.job_id, user_id=job.user_id)
            # No scope argument: the job row is the authority on its own
            # partition, and a worker holding only a job id has nothing better
            # to offer.
            await container.dream_service.run_job(job.job_id)

    # A worker that died mid-job leaves its row at `running` forever. Clearing
    # those on startup is housekeeping, not correctness — `find_active_for_scope`
    # already ignores them — but it means an operator sees `failed` with a reason
    # rather than rows stuck in flight.
    reaped = await container.jobs.reap_stale(
        stale_after_seconds=settings.dream.synthesis.job_stale_after_seconds
    )
    if reaped:
        logger.warning("stale_jobs_reaped", count=reaped)

    logger.info("synthesis_worker_ready", queue=queue.name)
    try:
        await queue.poll_forever(handle, stop=stop)
    finally:
        await container.close()
        logger.info("synthesis_worker_stopped")


async def _sweep_once() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    container = await DreamContainer.create(settings)
    try:
        result = await synthesise_everyone(container)
        print(
            f"Swept {result['processed']} users, {result['patternsCreated']} patterns, "
            f"{result['failed']} failed."
        )
    finally:
        await container.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Claim and run queued synthesis jobs (DREAM_QUEUE_PROVIDER=database)."
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="synthesise every user once and exit, instead of polling",
    )
    args = parser.parse_args()

    asyncio.run(_sweep_once() if args.sweep else run_forever())


if __name__ == "__main__":
    main()
