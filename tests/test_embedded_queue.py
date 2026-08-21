"""Synthesis in the API process, without a second service.

This is the default queue, so the bar for it is not "works in a demo" — it is
"a developer runs one command and nothing surprising happens under load or on
deploy". Three failures separate those, and each has a test here:

**Unbounded concurrency.** One task per job means fifty clicks of Run Dream is
fifty concurrent LLM streams. That is a rate-limit incident, and it arrives
exactly when the service is busiest.

**Rows that look in-flight but are not.** A `queued` or `running` row blocks that
user's next trigger for the whole stale timeout. Anything that abandons a job —
backpressure, shutdown — has to say so on the row, or it silently costs the user
fifteen minutes of memory that does not improve.

**Cancelling a deploy's worth of work.** Killing in-flight synthesis on shutdown
is both wasteful and leaves rows stuck. Draining first is cheap.
"""

from __future__ import annotations

import asyncio

import pytest

from app.models.dream import DreamJob
from app.models.enums import JobStatus
from app.providers.queue.inline_queue import EmbeddedJobQueue, InlineJobQueue


def _job(job_id: str) -> DreamJob:
    return DreamJob(job_id=job_id, user_id="alice")


class _Recorder:
    """Stands in for the job repository, recording failures."""

    def __init__(self) -> None:
        self.failed: dict[str, str] = {}

    async def fail(self, job_id: str, error: str) -> None:
        self.failed[job_id] = error


# --------------------------------------------------------------------------- #
# Detached from the request
# --------------------------------------------------------------------------- #


async def test_enqueue_returns_before_the_job_runs():
    """The whole reason a queue exists here. `POST /v1/memories` triggers
    synthesis and must not wait seconds for it."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(job_id: str) -> None:
        started.set()
        await release.wait()

    queue = EmbeddedJobQueue(runner)
    await queue.enqueue_synthesis(_job("job_1"))

    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert queue.depth == 1, "running, while the caller has already moved on"

    release.set()
    await queue.wait_for_all(timeout=5.0)
    await queue.close()


async def test_a_job_that_raises_does_not_stop_the_pool():
    """One bad job must not take the queue down with it."""
    seen: list[str] = []

    async def runner(job_id: str) -> None:
        seen.append(job_id)
        if job_id == "bad":
            raise RuntimeError("synthesis exploded")

    queue = EmbeddedJobQueue(runner)
    for job_id in ("bad", "good"):
        await queue.enqueue_synthesis(_job(job_id))
    await queue.wait_for_all(timeout=5.0)

    assert seen == ["bad", "good"] or set(seen) == {"bad", "good"}
    await queue.close()


# --------------------------------------------------------------------------- #
# Bounded concurrency
# --------------------------------------------------------------------------- #


async def test_concurrency_is_capped():
    """Fifty enqueues must not become fifty concurrent LLM streams."""
    concurrent = 0
    peak = 0
    release = asyncio.Event()

    async def runner(job_id: str) -> None:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await release.wait()
        concurrent -= 1

    queue = EmbeddedJobQueue(runner, max_concurrent=2)
    for index in range(10):
        await queue.enqueue_synthesis(_job(f"job_{index}"))

    # Give the pool a chance to over-schedule if it is going to.
    await asyncio.sleep(0.05)
    assert peak <= 2, f"pool ran {peak} jobs at once with max_concurrent=2"

    release.set()
    await queue.wait_for_all(timeout=5.0)
    assert peak == 2, "and it should actually use the capacity it has"
    await queue.close()


async def test_the_pool_starts_exactly_the_workers_it_was_asked_for():
    async def runner(job_id: str) -> None:
        return None

    queue = EmbeddedJobQueue(runner, max_concurrent=3)
    await queue.enqueue_synthesis(_job("job_1"))

    assert len(queue._workers) == 3  # noqa: SLF001 - the pool is what is under test
    await queue.close()


# --------------------------------------------------------------------------- #
# Backpressure
# --------------------------------------------------------------------------- #


async def test_a_saturated_queue_fails_the_job_rather_than_parking_it():
    """A row left at `queued` reads as in-flight, so it would block this user's
    next trigger for the whole stale timeout. Failing it frees them, and the
    trigger is derived from durable state, so the next write re-queues it."""
    release = asyncio.Event()
    jobs = _Recorder()

    async def runner(job_id: str) -> None:
        await release.wait()

    queue = EmbeddedJobQueue(max_concurrent=1, max_queued=1)
    queue.bind(runner, jobs)

    await queue.enqueue_synthesis(_job("running"))
    await asyncio.sleep(0)          # let the worker pick it up
    await queue.enqueue_synthesis(_job("waiting"))
    await queue.enqueue_synthesis(_job("rejected"))

    assert "rejected" in jobs.failed
    assert "saturated" in jobs.failed["rejected"]
    # And it says the work is not lost, because it is not.
    assert "retried" in jobs.failed["rejected"]
    assert "running" not in jobs.failed
    assert "waiting" not in jobs.failed

    release.set()
    await queue.wait_for_all(timeout=5.0)
    await queue.close()


async def test_rejection_without_a_repository_still_says_so():
    """Degrades to a log line rather than crashing when nothing was bound."""
    release = asyncio.Event()

    async def runner(job_id: str) -> None:
        await release.wait()

    queue = EmbeddedJobQueue(runner, max_concurrent=1, max_queued=1)
    await queue.enqueue_synthesis(_job("running"))
    await asyncio.sleep(0)
    await queue.enqueue_synthesis(_job("waiting"))

    assert await queue.enqueue_synthesis(_job("rejected")) == "rejected"

    release.set()
    await queue.wait_for_all(timeout=5.0)
    await queue.close()


# --------------------------------------------------------------------------- #
# Shutdown
# --------------------------------------------------------------------------- #


async def test_shutdown_drains_work_in_flight():
    """A deploy should not throw away a job that was two seconds from done."""
    finished: list[str] = []

    async def runner(job_id: str) -> None:
        await asyncio.sleep(0.05)
        finished.append(job_id)

    queue = EmbeddedJobQueue(runner, max_concurrent=1, drain_seconds=5.0)
    for index in range(3):
        await queue.enqueue_synthesis(_job(f"job_{index}"))

    await queue.close()

    assert finished == ["job_0", "job_1", "job_2"]


async def test_a_job_abandoned_at_shutdown_is_recorded():
    """Otherwise the row sits at `running` and blocks that user for fifteen
    minutes after a deploy that took thirty seconds."""
    jobs = _Recorder()

    async def runner(job_id: str) -> None:
        await asyncio.sleep(30)     # never finishes within the drain window

    queue = EmbeddedJobQueue(max_concurrent=1, drain_seconds=0.05)
    queue.bind(runner, jobs)
    await queue.enqueue_synthesis(_job("slow"))
    await asyncio.sleep(0)

    await queue.close()

    assert "slow" in jobs.failed
    assert "shutdown" in jobs.failed["slow"]


async def test_work_queued_but_never_started_is_recorded_too():
    jobs = _Recorder()

    async def runner(job_id: str) -> None:
        await asyncio.sleep(30)

    queue = EmbeddedJobQueue(max_concurrent=1, drain_seconds=0.05)
    queue.bind(runner, jobs)
    await queue.enqueue_synthesis(_job("started"))
    await asyncio.sleep(0)
    await queue.enqueue_synthesis(_job("never_started"))

    await queue.close()

    assert jobs.failed["never_started"] == "service stopped before it started"


async def test_enqueueing_during_shutdown_is_refused_not_dropped():
    jobs = _Recorder()

    async def runner(job_id: str) -> None:
        return None

    queue = EmbeddedJobQueue(max_concurrent=1)
    queue.bind(runner, jobs)
    await queue.close()

    await queue.enqueue_synthesis(_job("late"))
    assert "shutting down" in jobs.failed["late"]


async def test_closing_twice_is_safe():
    async def runner(job_id: str) -> None:
        return None

    queue = EmbeddedJobQueue(runner)
    await queue.enqueue_synthesis(_job("job_1"))
    await queue.close()
    await queue.close()


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


async def test_depth_reports_waiting_plus_running():
    """Surfaced in /ready. Saturation is a thing you want to see coming."""
    release = asyncio.Event()

    async def runner(job_id: str) -> None:
        await release.wait()

    queue = EmbeddedJobQueue(runner, max_concurrent=1, max_queued=10)
    for index in range(3):
        await queue.enqueue_synthesis(_job(f"job_{index}"))
    await asyncio.sleep(0)

    assert queue.depth == 3

    release.set()
    await queue.wait_for_all(timeout=5.0)
    assert queue.depth == 0
    await queue.close()


async def test_a_queue_with_no_runner_refuses_rather_than_silently_dropping():
    queue = EmbeddedJobQueue()
    with pytest.raises(RuntimeError, match="no runner"):
        await queue.enqueue_synthesis(_job("job_1"))


async def test_the_previous_name_still_works():
    """`InlineJobQueue` is what this was called when it ran one unbounded task
    per job. Tests, examples and eval scripts still import it."""
    assert InlineJobQueue is EmbeddedJobQueue


async def test_the_factory_sizes_the_pool_from_settings(settings):
    from app.providers.queue.factory import build_job_queue

    settings.dream.synthesis.max_concurrent = 5
    settings.dream.synthesis.max_queued = 7
    queue = build_job_queue(settings)

    assert isinstance(queue, EmbeddedJobQueue)
    assert queue._max_concurrent == 5  # noqa: SLF001
    assert queue._max_queued == 7  # noqa: SLF001


async def test_it_is_the_default_so_one_process_is_enough():
    """The reason this module exists: `uvicorn app.main:app` and nothing else."""
    from app.core.config import Settings

    assert Settings(env="test", identity_salt="t").queue_provider == "inline"


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #


async def test_synthesis_runs_through_the_container_with_no_second_process(
    container, scope, store_memory
):
    """The whole point, exercised through the real wiring rather than a stub."""
    for index in range(5):
        await store_memory(f"User does activity {index} regularly.")

    job = await container.dream_service.request_synthesis(scope, requested_by="test")
    await container.queue.wait_for_all(timeout=30.0)

    finished = await container.jobs.get(job.job_id)
    assert finished.status in (JobStatus.COMPLETED, JobStatus.SKIPPED, JobStatus.FAILED)
    assert finished.status is not JobStatus.QUEUED, "nothing picked the job up"
