"""Forgetting, done safely.

MemDream's first rule is that nothing is ever deleted. "Remove the memories that
no longer matter" looks like it breaks that rule. It does not, because there is
already a status that means exactly the right thing:

    expired   the row is kept and leaves the search index, so the memory stops
              influencing answers but stays auditable

So consolidation forgets by **transitioning**, never by deleting. Same mechanism
as a user saying "forget that", same audit trail, and — the part that matters —
**reversible**. Any heuristic that decides what no longer matters will sometimes
be wrong, and the cost of being wrong has to be a status flip rather than a
gone-forever row.

## What this pass does today

One thing, and it needs no model and no judgement: **a memory whose validity
window has closed is provably no longer true.** When Dream supersedes a fact it
sets `validUntil` on the old row; extraction sets it directly for anything the
user bounded themselves ("I'm on leave until Friday"). Once that instant passes,
the memory is not stale or unpopular — it is false, and it is still sitting in
the active set competing for candidate slots.

That is free to find and unambiguous to act on, which is why it ships first.

## The second rule, and why it ships switched off

Decay on disuse — retiring what nothing has recalled — is implemented here but
**disabled by default**, and it should stay that way until you have watched it.

A closed validity window is a *fact*. Disuse is a *judgement*, and a judgement
needs evidence. The evidence is `lastRecalledAt`, which only starts accumulating
the day recall tracking is switched on — before that every row reads
`null`, which is indistinguishable from "nothing has ever wanted this". Enable
decay on day one and the entire database looks maximally stale in a single pass.

`recall_epoch` is the guard: a memory becomes eligible only once the decay
window has elapsed *since tracking began*. Until then `_decay_cutoff` returns
None and the rule cannot fire at all, whatever else is configured.

While it is off, candidates are still identified and logged as
`consolidation_would_expire`. That is the point — you gather weeks of evidence,
set the thresholds from what you actually see, and only then turn it on.

## The guard rails

Every one is a veto, and each exists because the alternative is a specific,
silent, expensive mistake:

    never expire   importance at or above the floor — an allergy nobody has
                   asked about in a year still outranks yesterday's lunch order
    never expire   temporalNature: permanent
    never expire   anything listed in an active pattern's sourceMemoryIds —
                   expire the evidence and the pattern becomes an unsupported
                   assertion, which validation already refuses to create
    never expire   anything younger than the window — new is not unwanted

The asymmetry is the same one behind the triage gate: a wrong expiry is silent
and expensive, a missed one costs a few tokens on the next decision. Act only on
certainty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.core.config import ConsolidationSettings, Settings, get_settings
from app.core.logging import get_logger
from app.memory.lifecycle import MemoryLifecycle
from app.models.enums import MemoryStatus, MemoryType, TemporalNature
from app.models.memory import Memory, utcnow
from app.models.scope import MemoryScope
from app.repositories.base import MemoryRepository

logger = get_logger(__name__)


@dataclass(slots=True)
class ConsolidationResult:
    """What one pass over a user's memories changed."""

    scanned: int = 0
    lapsed: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    #: Memories the decay rule *would* retire. Reported even when decay is off,
    #: which is the point: it is how you gather evidence for a threshold before
    #: trusting it with real data.
    decayed: list[str] = field(default_factory=list)
    #: What was actually retired. Kept separate from the two candidate lists on
    #: purpose: with decay in measurement mode `decayed` is full and this is
    #: empty, and a count that conflated them would report a sweep expiring
    #: memories it had deliberately left alone.
    expired: list[str] = field(default_factory=list)

    @property
    def expired_count(self) -> int:
        return len(self.expired)

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "expired": len(self.expired),
            "lapsed": len(self.lapsed),
            "decayed": len(self.decayed),
            "expiredIds": self.expired[:20],
            "protected": len(self.protected),
        }


class ConsolidationEngine:
    """Retires memories that are provably no longer true.

    Deterministic throughout — no LLM call, no embedding. A nightly pass over a
    thousand users costs a thousand indexed queries, not a thousand model calls,
    and that is the property to preserve as more rules are added.
    """

    def __init__(
        self,
        repository: MemoryRepository,
        lifecycle: MemoryLifecycle,
        settings: Settings | None = None,
    ) -> None:
        self._repository = repository
        self._lifecycle = lifecycle
        self._settings = settings or get_settings()

    async def run(
        self,
        scope: MemoryScope,
        *,
        now: datetime | None = None,
        dry_run: bool = False,
        config: ConsolidationSettings | None = None,
    ) -> ConsolidationResult:
        """Consolidate one user's memories.

        ``dry_run`` reports what would be expired without touching anything,
        which is how you check a threshold before trusting it with real data.

        ``config`` overrides the configured thresholds **for this call only**.
        It exists so an API request or a CLI flag can try a different window
        without mutating process-wide settings — which, in a server handling
        several tenants at once, would silently change the rules for everybody
        else's pass.
        """
        now = now or utcnow()
        config = config or self._settings.dream.consolidation
        result = ConsolidationResult()

        active = await self._repository.find(
            scope,
            statuses=[MemoryStatus.ACTIVE],
            limit=config.max_per_run,
            include_embedding=False,
        )
        result.scanned = len(active)
        if not active:
            return result

        protected = await self._pattern_evidence(scope)

        epoch = self._recall_epoch(config)

        for memory in active:
            if memory.memory_type == MemoryType.PATTERN.value:
                # A pattern is synthesis output, not an observation with a shelf
                # life. Retiring one belongs to synthesis, which knows whether
                # its evidence still holds.
                continue

            lapsed = memory.valid_until is not None and memory.valid_until <= now
            decayed = epoch is not None and self._has_decayed(memory, now, epoch, config)
            if not (lapsed or decayed):
                continue

            if memory.memory_id in protected:
                # Evidence under an active pattern. Expiring it would leave the
                # pattern asserting something with nothing behind it.
                result.protected.append(memory.memory_id)
                continue

            # A closed validity window is a fact; disuse is a judgement. They
            # are counted apart so the log can say which rule fired, and so
            # turning decay on can be evaluated without the lapsed count
            # muddying the numbers.
            (result.lapsed if lapsed else result.decayed).append(memory.memory_id)

        doomed = result.lapsed + (result.decayed if config.decay_enabled else [])
        if result.decayed and not config.decay_enabled:
            # Measurement mode. Say what would have happened and change nothing.
            logger.info(
                "consolidation_would_expire",
                count=len(result.decayed),
                after_days=config.decay_after_days,
                importance_floor=config.decay_importance_floor,
                sample=result.decayed[:5],
            )

        if doomed and not dry_run:
            for memory_id in doomed:
                memory = await self._repository.get(scope, memory_id)
                if memory is not None:
                    # Through the lifecycle rather than the repository, so the
                    # vector index is updated and dependent patterns are flagged
                    # for re-evaluation — the same path "forget that" takes.
                    await self._lifecycle.forget(scope, memory)
                    result.expired.append(memory_id)

        if result.lapsed or result.decayed or result.protected:
            logger.info(
                "consolidation_finished",
                scanned=result.scanned,
                lapsed=len(result.lapsed),
                decayed=len(result.decayed),
                expired=len(result.expired),
                protected=len(result.protected),
                decay_enabled=config.decay_enabled,
                dry_run=dry_run,
            )
        return result

    # --- recall decay ---------------------------------------------------- #

    def _recall_epoch(self, config: ConsolidationSettings) -> datetime | None:
        """When recall tracking began, or None if it has not been declared.

        This is the cold-start guard, and it is required. Every memory written
        before tracking existed carries `lastRecalledAt: null`, which reads
        identically to "nothing has ever wanted this" — so on day one that is
        true of the entire database. The epoch is what separates *we looked and
        nobody used it* from *we were not looking*.

        Unparseable fails closed: a typo in a date must not become a mass
        expiry.
        """
        raw = config.recall_epoch
        if not raw:
            return None
        try:
            epoch = datetime.fromisoformat(raw)
        except ValueError:
            logger.warning("recall_epoch_unparseable", value=raw[:32])
            return None
        return epoch if epoch.tzinfo else epoch.replace(tzinfo=timezone.utc)

    def _has_decayed(
        self,
        memory: Memory,
        now: datetime,
        epoch: datetime,
        config: ConsolidationSettings,
    ) -> bool:
        """Whether disuse has earned this memory a retirement.

        Every guard is a veto, and each exists because the alternative is a
        specific, silent, expensive mistake.
        """
        # Importance outranks disuse, always. This is the line that keeps a
        # peanut allergy in the active set through a year of nobody asking.
        if memory.importance >= config.decay_importance_floor:
            return False

        # `permanent` means the fact cannot change by its nature. How often it
        # is read has no bearing on that.
        if memory.temporal_nature is TemporalNature.PERMANENT:
            return False

        # The last moment we have evidence this memory was wanted — or, failing
        # that, the last moment we could have observed that it was.
        #
        # The epoch is a floor rather than a global gate: a memory written two
        # years ago and never recalled has still only been *observed* since
        # tracking started, so its window runs from there. That way a short
        # window works from the day it is configured, instead of the whole rule
        # sitting inert until the window has elapsed once.
        observed_from = max(memory.created_at, epoch)
        last_wanted = memory.last_recalled_at or observed_from

        window = timedelta(days=config.decay_after_days)
        return (now - last_wanted) >= window and (now - observed_from) >= window

    async def _pattern_evidence(self, scope: MemoryScope) -> set[str]:
        """Every memory an active pattern rests on.

        Read once per pass rather than per memory: a user has a handful of
        patterns and potentially hundreds of memories, so this is one query
        against N.
        """
        patterns = await self._repository.find_patterns(scope, only_active=True)
        return {
            source_id for pattern in patterns for source_id in pattern.source_memory_ids
        }
