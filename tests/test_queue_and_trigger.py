"""MongoDB-backed queue, stale-job reaping, and threshold auto-triggering.

The property under test throughout: MemDream must run correctly with MongoDB as
its only infrastructure. No broker, and no scheduled sweep.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.dream import DreamJob
from app.models.enums import JobStatus
from app.models.memory import utcnow

pytestmark = pytest.mark.asyncio


async def _job(container, scope, status=JobStatus.QUEUED, **overrides) -> DreamJob:
    job = DreamJob(
        client_id=scope.client_id, bot_id=scope.bot_id, user_id=scope.user_id, status=status
    )
    for key, value in overrides.items():
        setattr(job, key, value)
    await container.jobs.create(job)
    return job


# --------------------------------------------------------------------------- #
# Stale jobs — the denial-of-service bug
# --------------------------------------------------------------------------- #


async def test_a_fresh_running_job_blocks_a_duplicate(container, scope):
    await _job(container, scope, JobStatus.RUNNING, started_at=utcnow())
    assert await container.jobs.find_active_for_scope(scope) is not None


async def test_an_abandoned_job_does_not_block_forever(container, scope):
    """Regression: a worker dying mid-synthesis left its row at `running`, and
    the deduplication guard then blocked that user's synthesis permanently —
    turning a safety check into a denial of service."""
    await _job(
        container, scope, JobStatus.RUNNING, started_at=utcnow() - timedelta(hours=2)
    )
    assert await container.jobs.find_active_for_scope(scope, stale_after_seconds=900) is None


async def test_a_job_that_never_started_is_judged_on_creation_time(container, scope):
    await _job(container, scope, JobStatus.QUEUED, created_at=utcnow() - timedelta(hours=2))
    assert await container.jobs.find_active_for_scope(scope, stale_after_seconds=900) is None


async def test_reaping_marks_abandoned_jobs_failed(container, scope):
    stale = await _job(
        container, scope, JobStatus.RUNNING, started_at=utcnow() - timedelta(hours=2)
    )
    fresh = await _job(container, scope, JobStatus.RUNNING, started_at=utcnow())

    reaped = await container.jobs.reap_stale(stale_after_seconds=900)

    assert reaped == 1
    assert (await container.jobs.get(stale.job_id)).status == JobStatus.FAILED
    assert "abandoned" in (await container.jobs.get(stale.job_id)).error
    assert (await container.jobs.get(fresh.job_id)).status == JobStatus.RUNNING


async def test_a_stale_job_no_longer_blocks_a_new_request(container, scope):
    await _job(container, scope, JobStatus.RUNNING, started_at=utcnow() - timedelta(hours=2))
    job = await container.dream_service.request_synthesis(scope, wait=True)
    assert job.status in (JobStatus.COMPLETED, JobStatus.SKIPPED)


# --------------------------------------------------------------------------- #
# MongoDB as the queue
# --------------------------------------------------------------------------- #


async def test_claim_returns_a_queued_job_and_marks_it_running(container, scope):
    job = await _job(container, scope, JobStatus.QUEUED)

    claimed = await container.jobs.claim_next("worker-1")

    assert claimed is not None
    assert claimed.job_id == job.job_id
    assert claimed.status == JobStatus.RUNNING
    assert claimed.metadata.get("claimedBy") == "worker-1"


async def test_two_workers_cannot_claim_the_same_job(container, scope):
    """The whole reason MongoDB can act as the queue: find_one_and_update is an
    atomic compare-and-swap, so the second worker's filter no longer matches."""
    await _job(container, scope, JobStatus.QUEUED)

    first = await container.jobs.claim_next("worker-1")
    second = await container.jobs.claim_next("worker-2")

    assert first is not None
    assert second is None


async def test_claim_returns_none_when_the_queue_is_empty(container):
    assert await container.jobs.claim_next("worker-1") is None


async def test_mongo_queue_enqueue_is_a_no_op_because_the_row_is_the_queue(container, scope):
    from app.providers.queue.mongo_queue import MongoJobQueue

    queue = MongoJobQueue(container.jobs)
    job = await _job(container, scope, JobStatus.QUEUED)

    assert await queue.enqueue_synthesis(job) == job.job_id
    claimed = await queue.claim_next()
    assert claimed.job_id == job.job_id


# --------------------------------------------------------------------------- #
# Threshold auto-triggering
# --------------------------------------------------------------------------- #


async def test_no_trigger_below_the_threshold(container, scope, store_memory):
    container.settings.dream.synthesis.auto_trigger_new_memories = 20
    for i in range(3):
        await store_memory(f"Memory number {i}.")

    assert await container.dream_service.maybe_trigger(scope) is None


async def test_trigger_fires_once_the_threshold_is_crossed(container, scope, store_memory):
    config = container.settings.dream.synthesis
    config.auto_trigger_new_memories = 5
    config.auto_trigger_cooldown_seconds = 0

    for i in range(6):
        await store_memory(f"User does activity number {i} regularly.")

    job = await container.dream_service.maybe_trigger(scope)

    assert job is not None
    assert job.requested_by == "auto-trigger"


async def test_cooldown_suppresses_a_second_trigger(container, scope, store_memory):
    config = container.settings.dream.synthesis
    config.auto_trigger_new_memories = 3
    config.auto_trigger_cooldown_seconds = 3600

    for i in range(4):
        await store_memory(f"User does activity {i}.")
    await _job(container, scope, JobStatus.COMPLETED, completed_at=utcnow())

    assert await container.dream_service.maybe_trigger(scope) is None


async def test_only_memories_since_the_last_run_count(container, scope, store_memory):
    """Total count would re-fire forever once the threshold was crossed once."""
    config = container.settings.dream.synthesis
    config.auto_trigger_new_memories = 5
    config.auto_trigger_cooldown_seconds = 0

    for i in range(10):
        await store_memory(f"Old memory {i}.", created_at=utcnow() - timedelta(days=2))
    await _job(container, scope, JobStatus.COMPLETED, completed_at=utcnow() - timedelta(days=1))

    # Ten memories exist, but none are new since the last run.
    assert await container.dream_service.maybe_trigger(scope) is None


async def test_concurrency_cap_holds_back_new_triggers(container, scope, store_memory, other_scope):
    config = container.settings.dream.synthesis
    config.auto_trigger_new_memories = 2
    config.auto_trigger_cooldown_seconds = 0
    config.auto_trigger_max_concurrent = 1

    await _job(container, other_scope, JobStatus.RUNNING, started_at=utcnow())
    for i in range(3):
        await store_memory(f"User does activity {i}.")

    assert await container.dream_service.maybe_trigger(scope) is None


async def test_auto_trigger_can_be_disabled(container, scope, store_memory):
    container.settings.dream.synthesis.auto_trigger = False
    container.settings.dream.synthesis.auto_trigger_new_memories = 1
    await store_memory("A memory.")

    assert await container.dream_service.maybe_trigger(scope) is None


async def test_trigger_failure_never_breaks_the_write(container, scope, monkeypatch):
    """Synthesis is a background nicety. A failure here must not fail the add."""

    async def boom(*args, **kwargs):
        raise RuntimeError("mongo is down")

    monkeypatch.setattr(container.jobs, "running_count", boom)
    assert await container.dream_service.maybe_trigger(scope) is None


async def test_counting_new_memories_excludes_patterns(container, scope, store_memory):
    """Patterns are synthesis output. Counting them would let synthesis
    re-trigger itself in a loop."""
    from app.models.enums import MemoryType

    await store_memory("A real memory.")
    await store_memory("A synthesised pattern.", memory_type=MemoryType.PATTERN.value)

    assert await container.memories.count_since(scope, None) == 1
