"""Phases 2 and 3 — decay on disuse, and the nightly clock.

Decay ships **switched off**, and the most important tests here are the ones
proving it cannot fire by accident.

The trap it exists to avoid is worth stating plainly. `lastRecalledAt` only
starts accumulating the day recall tracking is switched on. Before that every
row reads `null`, which is indistinguishable from *nothing has ever wanted
this* — so a deployment that enables decay on day one would look at a database
where everything appears maximally stale and retire all of it in one pass.
`recall_epoch` is the guard, and `test_decay_cannot_fire_before_the_epoch` is
the test that says so.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.dream.consolidation import ConsolidationEngine
from app.dream.scheduler import ConsolidationScheduler
from app.models.enums import MemoryStatus, MemoryType, TemporalNature
from app.models.memory import Memory, utcnow

pytestmark = pytest.mark.asyncio

LONG_AGO = utcnow() - timedelta(days=400)


@pytest.fixture
def decaying(container):
    """Consolidation with decay switched on and the epoch long past."""
    config = container.settings.dream.consolidation
    config.decay_enabled = True
    config.decay_after_days = 90
    config.decay_importance_floor = 0.5
    config.recall_epoch = (utcnow() - timedelta(days=400)).date().isoformat()
    return ConsolidationEngine(container.memories, container.lifecycle, container.settings)


async def _store(container, scope, content, **overrides):
    overrides.setdefault("created_at", LONG_AGO)
    memory = Memory(
        **scope.as_model_fields(),
        content=content,
        embedding=await container.embeddings.embed(content),
        embedding_model=container.embeddings.model_name,
        **overrides,
    )
    await container.lifecycle.activate(scope, memory)
    return memory


# --------------------------------------------------------------------------- #
# It cannot fire by accident
# --------------------------------------------------------------------------- #


async def test_decay_is_off_by_default(container):
    config = container.settings.dream.consolidation
    assert config.decay_enabled is False
    assert config.recall_epoch == "", "no epoch means the rule cannot run at all"


async def test_decay_cannot_fire_before_the_epoch(container, scope):
    """The cold-start guard, and the reason this feature is safe to ship.

    Without it, the day you enable decay is the day every pre-existing memory —
    all of them reading `lastRecalledAt: null` — gets retired at once.
    """
    config = container.settings.dream.consolidation
    config.decay_enabled = True
    config.decay_after_days = 90
    # Tracking started yesterday. However old the memory is, ninety days of
    # silence cannot have been *observed* yet — its window runs from the epoch.
    config.recall_epoch = (utcnow() - timedelta(days=1)).date().isoformat()

    engine = ConsolidationEngine(container.memories, container.lifecycle, container.settings)
    stale = await _store(container, scope, "User once mentioned liking jazz.", importance=0.3)

    result = await engine.run(scope)

    assert result.decayed == []
    assert (await container.memories.get(scope, stale.memory_id)).status is MemoryStatus.ACTIVE


async def test_an_empty_epoch_disables_decay_outright(container, scope):
    config = container.settings.dream.consolidation
    config.decay_enabled = True
    config.recall_epoch = ""

    engine = ConsolidationEngine(container.memories, container.lifecycle, container.settings)
    await _store(container, scope, "User once mentioned liking jazz.", importance=0.3)

    assert (await engine.run(scope)).decayed == []


async def test_an_unparseable_epoch_fails_closed(container, scope):
    """A typo in a date must not become a mass expiry."""
    config = container.settings.dream.consolidation
    config.decay_enabled = True
    config.recall_epoch = "last tuesday"

    engine = ConsolidationEngine(container.memories, container.lifecycle, container.settings)
    await _store(container, scope, "User once mentioned liking jazz.", importance=0.3)

    assert (await engine.run(scope)).decayed == []


# --------------------------------------------------------------------------- #
# Measurement mode — the weeks before you trust it
# --------------------------------------------------------------------------- #


async def test_candidates_are_identified_while_decay_is_off(container, scope):
    """The whole point of shipping it disabled: you collect evidence for a
    threshold instead of guessing at one."""
    config = container.settings.dream.consolidation
    config.decay_enabled = False
    config.recall_epoch = (utcnow() - timedelta(days=400)).date().isoformat()

    engine = ConsolidationEngine(container.memories, container.lifecycle, container.settings)
    stale = await _store(container, scope, "User once mentioned liking jazz.", importance=0.3)

    result = await engine.run(scope)

    assert stale.memory_id in result.decayed, "it must still say what it would do"
    assert (
        await container.memories.get(scope, stale.memory_id)
    ).status is MemoryStatus.ACTIVE, "and change nothing"


# --------------------------------------------------------------------------- #
# The rule, once enabled
# --------------------------------------------------------------------------- #


async def test_an_unused_low_value_memory_decays(decaying, container, scope):
    stale = await _store(container, scope, "User once mentioned liking jazz.", importance=0.3)

    assert stale.memory_id in (await decaying.run(scope)).decayed
    assert (await container.memories.get(scope, stale.memory_id)).status is MemoryStatus.EXPIRED


async def test_importance_outranks_disuse(decaying, container, scope):
    """The line that keeps a peanut allergy in the active set through a year of
    nobody asking about peanuts."""
    allergy = await _store(container, scope, "User is allergic to peanuts.", importance=0.9)

    assert allergy.memory_id not in (await decaying.run(scope)).decayed
    assert (await container.memories.get(scope, allergy.memory_id)).status is MemoryStatus.ACTIVE


async def test_permanent_facts_never_decay(decaying, container, scope):
    born = await _store(
        container, scope, "User was born in Chennai.",
        importance=0.3, temporal_nature=TemporalNature.PERMANENT,
    )

    assert born.memory_id not in (await decaying.run(scope)).decayed


async def test_a_recently_recalled_memory_survives(decaying, container, scope):
    """This is what Phase 0 was for."""
    used = await _store(container, scope, "User once mentioned liking jazz.", importance=0.3)
    await container.memories.record_recall(scope, [used.memory_id])

    assert used.memory_id not in (await decaying.run(scope)).decayed


async def test_a_young_memory_is_new_not_unwanted(decaying, container, scope):
    fresh = await _store(
        container, scope, "User mentioned liking jazz.",
        importance=0.3, created_at=utcnow() - timedelta(days=2),
    )

    assert fresh.memory_id not in (await decaying.run(scope)).decayed


async def test_pattern_evidence_survives_decay_too(decaying, container, scope):
    evidence = await _store(container, scope, "User ran on Monday.", importance=0.3)
    await _store(
        container, scope, "User exercises most mornings.",
        memory_type=MemoryType.PATTERN.value,
        source_memory_ids=[evidence.memory_id],
        explicit=False,
    )

    result = await decaying.run(scope)

    assert evidence.memory_id in result.protected
    assert evidence.memory_id not in result.decayed


async def test_lapsed_and_decayed_are_counted_apart(decaying, container, scope):
    """A closed validity window is a fact; disuse is a judgement. Mixing the
    counts would make it impossible to evaluate turning decay on."""
    lapsed = await _store(
        container, scope, "User is on leave this week.",
        importance=0.3, valid_until=utcnow() - timedelta(days=1),
    )
    unused = await _store(container, scope, "User once mentioned liking jazz.", importance=0.3)

    result = await decaying.run(scope)

    assert lapsed.memory_id in result.lapsed
    assert unused.memory_id in result.decayed
    assert result.as_dict()["lapsed"] == 1
    assert result.as_dict()["decayed"] == 1


async def test_decay_needs_no_model(decaying, container, scope, llm):
    await _store(container, scope, "User once mentioned liking jazz.", importance=0.3)
    before = len(llm.calls)

    await decaying.run(scope)

    assert len(llm.calls) == before


# --------------------------------------------------------------------------- #
# Phase 3 — the nightly clock
# --------------------------------------------------------------------------- #


def _scheduler(container) -> ConsolidationScheduler:
    return ConsolidationScheduler(container, container.settings)


async def test_it_picks_the_next_occurrence_of_the_configured_hour(container):
    container.settings.dream.consolidation.schedule_hour_utc = 3
    scheduler = _scheduler(container)

    before = datetime(2026, 5, 10, 1, 0, tzinfo=timezone.utc)
    after = datetime(2026, 5, 10, 5, 0, tzinfo=timezone.utc)

    assert scheduler._next_run(before) == datetime(2026, 5, 10, 3, 0, tzinfo=timezone.utc)
    assert scheduler._next_run(after) == datetime(2026, 5, 11, 3, 0, tzinfo=timezone.utc)


async def test_a_sweep_covers_every_partition(container, scope):
    """The reason this exists: consolidation was only reachable through a
    synthesis run, so an account that stopped talking never got one."""
    from app.models.scope import MemoryScope

    await _store(
        container, scope, "User is on leave this week.",
        valid_until=utcnow() - timedelta(days=1),
    )
    other = MemoryScope(user_id="someone-else")
    lapsed_elsewhere = await _store(
        container, other, "User is travelling this week.",
        valid_until=utcnow() - timedelta(days=1),
    )

    totals = await _scheduler(container).run_once()

    assert totals["partitions"] >= 2
    assert totals["expired"] >= 2
    assert (
        await container.memories.get(other, lapsed_elsewhere.memory_id)
    ).status is MemoryStatus.EXPIRED


async def test_one_bad_partition_does_not_stop_the_sweep(container, scope, monkeypatch):
    calls = {"n": 0}
    original = container.consolidation.run

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("that user's data is broken")
        return await original(*args, **kwargs)

    monkeypatch.setattr(container.consolidation, "run", flaky)

    from app.models.scope import MemoryScope

    await _store(container, scope, "User likes tea.")
    await _store(container, MemoryScope(user_id="someone-else"), "User likes coffee.")

    totals = await _scheduler(container).run_once()

    assert totals["failed"] == 1
    assert calls["n"] >= 2, "it kept going"


async def test_the_schedule_can_be_switched_off(container):
    container.settings.dream.consolidation.schedule_enabled = False
    scheduler = _scheduler(container)

    scheduler.start()

    assert scheduler._task is None
    await scheduler.stop()


async def test_stopping_is_immediate_and_safe(container):
    """Waiting on the stop event rather than sleeping means shutdown does not
    have to wait up to a day."""
    container.settings.dream.consolidation.schedule_enabled = True
    scheduler = _scheduler(container)

    scheduler.start()
    assert scheduler._task is not None
    await scheduler.stop()

    assert scheduler._task is None
    await scheduler.stop()  # idempotent


async def test_measurement_mode_does_not_inflate_the_expiry_count(container, scope):
    """The accounting trap. With decay off, `decayed` is full of candidates that
    were deliberately left alone — a count that conflated them would report a
    nightly sweep expiring memories it never touched."""
    config = container.settings.dream.consolidation
    config.decay_enabled = False
    config.recall_epoch = (utcnow() - timedelta(days=400)).date().isoformat()

    engine = ConsolidationEngine(container.memories, container.lifecycle, container.settings)
    await _store(container, scope, "User once mentioned liking jazz.", importance=0.3)

    result = await engine.run(scope)

    assert result.decayed, "a candidate was identified"
    assert result.expired == [], "and nothing was actually retired"
    assert result.expired_count == 0
    assert result.as_dict()["expired"] == 0


async def test_a_dry_run_reports_candidates_but_expires_nothing(decaying, container, scope):
    lapsed = await _store(
        container, scope, "User is on leave this week.",
        valid_until=utcnow() - timedelta(days=1),
    )

    result = await decaying.run(scope, dry_run=True)

    assert lapsed.memory_id in result.lapsed
    assert result.expired == []
    assert (await container.memories.get(scope, lapsed.memory_id)).status is MemoryStatus.ACTIVE
