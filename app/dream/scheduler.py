"""The nightly pass.

Consolidation is currently reachable only through a synthesis run, and synthesis
auto-triggers on twenty new memories with a six-hour cooldown. A user who stops
talking never gets consolidated — which is exactly the account most in need of
it, because it is the one accumulating memories that have quietly stopped being
true.

So this is a clock. It wakes once a day, walks every partition that has
memories, and consolidates each one.

## Why it lives in the API process

Because everything else does. MemDream deploys as one process and one database,
and adding a cron container to run a query that costs nothing would undo that
for no benefit. It is an asyncio task: it sleeps, it wakes, it works, it sleeps.

The pass is deterministic throughout — no model, no embedding. A thousand users
is a thousand indexed queries, not a thousand LLM calls, which is what makes a
nightly sweep affordable at all. Guard that property if you add rules to it.

## What happens with several replicas

They all run it, and that is harmless: consolidation is idempotent. Expiring an
already-expired memory does nothing, because `mark_expired` only moves rows that
are still `active`. The work is repeated, not doubled.

If you would rather it ran once, put the schedule behind
`DREAM_QUEUE_PROVIDER=database` and a single worker — but for a job this cheap,
duplicate work is a smaller problem than the coordination needed to avoid it.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import date, datetime, time, timedelta, timezone

from app.core.config import Settings, get_settings
from app.core.logging import get_logger, operation_context
from app.models.memory import utcnow
from app.models.scope import MemoryScope

logger = get_logger(__name__)


class ConsolidationScheduler:
    """Runs a consolidation pass once a day, in this process."""

    def __init__(self, container, settings: Settings | None = None) -> None:
        self._container = container
        self._settings = settings or get_settings()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        #: Guards against a second run in the same UTC day — a restart at 00:05
        #: should not repeat the pass that just finished.
        self._last_run_on: date | None = None

    # --- lifecycle ------------------------------------------------------------ #

    def start(self) -> None:
        config = self._settings.dream.consolidation
        if not (config.enabled and config.schedule_enabled):
            logger.info("consolidation_schedule_disabled")
            return
        self._task = asyncio.create_task(self._loop(), name="consolidation-scheduler")
        logger.info("consolidation_scheduled", hour_utc=config.schedule_hour_utc)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    # --- the clock ------------------------------------------------------------ #

    def _next_run(self, now: datetime) -> datetime:
        """The next occurrence of the configured hour, in UTC."""
        hour = self._settings.dream.consolidation.schedule_hour_utc
        today = datetime.combine(now.date(), time(hour=hour), tzinfo=timezone.utc)
        return today if today > now else today + timedelta(days=1)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            now = utcnow()
            delay = (self._next_run(now) - now).total_seconds()
            try:
                # Waiting on the stop event rather than sleeping means shutdown
                # is immediate instead of up to a day late.
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return  # stop was set
            except asyncio.TimeoutError:
                pass

            today = utcnow().date()
            if self._last_run_on == today:
                continue
            self._last_run_on = today

            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad night must not stop the clock
                logger.error("consolidation_sweep_failed", error=str(exc)[:300])

    # --- the work ------------------------------------------------------------- #

    async def run_once(self) -> dict[str, int]:
        """Consolidate every partition that has memories.

        Sequential on purpose. There is no rush — the whole point of running at
        night is that nobody is waiting — and a burst of parallel queries would
        be the thing that slows down the request path that users *are* waiting on.
        """
        with operation_context():
            scopes = await self._container.memories.distinct_scopes()
            totals = {
                "partitions": len(scopes), "scanned": 0, "expired": 0,
                "compacted": 0, "absorbed": 0, "failed": 0,
            }
            logger.info("consolidation_sweep_started", partitions=len(scopes))

            for entry in scopes:
                scope = MemoryScope(
                    user_id=entry["userId"],
                    agent_id=entry.get("agentId"),
                    client_id=entry.get("clientId"),
                    namespace=entry.get("namespace") or {},
                )
                try:
                    result = await self._container.consolidation.run(scope)
                    totals["scanned"] += result.scanned
                    totals["expired"] += result.expired_count

                    # Retire what is dead first, then compress what is left —
                    # no point spending a model call summarising memories that
                    # were about to be expired anyway.
                    if self._settings.dream.compaction.enabled:
                        folded = await self._container.compaction.run(scope)
                        totals["compacted"] += len(folded.compacted)
                        totals["absorbed"] += folded.absorbed
                except Exception as exc:  # noqa: BLE001 - one user must not stop the sweep
                    totals["failed"] += 1
                    logger.error(
                        "consolidation_user_failed",
                        user_id=entry["userId"],
                        error=str(exc)[:200],
                    )

            logger.info("consolidation_sweep_finished", **totals)
            return totals
