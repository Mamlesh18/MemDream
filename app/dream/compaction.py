"""Folding many small memories into one that says the same thing.

A user who has ordered from you two hundred times has two hundred memories that
each say roughly *"User ordered a dosa on a Tuesday"*. None of them is wrong,
none is important on its own, and collectively they crowd out the ten that
matter — every decision pays to look at them, and the candidate shortlist has
only ten slots.

Compaction folds a cluster of those into one memory that carries what they
jointly say.

## Why this is MERGE, not a new concept

MemDream already has the exact semantics: `merged` means *absorbed into a
canonical row that says the same thing better*, the originals keep a
`mergedInto` pointer, and the canonical row inherits their `sourceMemoryIds`.
Compaction is that operation applied for volume rather than for duplication, so
it reuses `MemoryLifecycle.merge` verbatim rather than inventing a fifth status.

The consequence is the one that matters: **nothing is lost.** Every original is
retained and still readable, still traceable from the summary that replaced it.
If a summary turns out to be a bad one, the inputs are all still there.

## Why it is the riskiest thing here

Everything else in consolidation is deterministic. This asks a model to compress
several facts into one, and compression can drop something — which is why:

* it only ever compacts memories **below the importance floor**, so an allergy
  or a delivery address is never an input;
* it never touches pattern evidence, `permanent` facts, or anything recalled
  recently;
* the prompt is told, in as many words, that losing a fact is failure;
* it is **off by default**, and there is a dry run.

It is also the only part of a nightly pass that costs a model call, one per
cluster. At a thousand users that is a real bill, so it is gated on a user
having enough low-value memories to be worth compacting at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core.config import CompactionSettings, Settings, get_settings
from app.core.exceptions import LLMResponseError, ProviderError
from app.core.logging import get_logger
from app.core.metrics import OP_SYNTHESIZE, operation
from app.dream.clustering import cluster_memories
from app.dream.prompts import _dumps, _memory_view
from app.memory.lifecycle import MemoryLifecycle
from app.models.dream import CompactionResult as CompactionProposal
from app.models.enums import MemoryStatus, MemoryType, TemporalNature
from app.models.memory import Memory, utcnow
from app.models.scope import MemoryScope
from app.providers.embeddings.base import EmbeddingProvider
from app.providers.llm.base import LLMProvider
from app.repositories.base import MemoryRepository

logger = get_logger(__name__)


COMPACTION_SYSTEM = """\
Fold several of a user's memories into one that says everything they say.

Losing a fact is failure. Every name, number, dish, date and preference in the
inputs must survive into the summary. Do not generalise beyond them, do not
add anything, do not drop the unusual one because it does not fit.

Write one self-contained sentence or two, third person about "User", in the
same register as the inputs.

If the memories do not belong together — different subjects, unrelated facts —
return keep: false and no summary. Storing them separately is the right answer.\
"""


@dataclass(slots=True)
class CompactionOutcome:
    clusters: int = 0
    compacted: list[str] = field(default_factory=list)
    absorbed: int = 0
    rejected: int = 0
    #: Set when the model declined, or when validation refused its answer.
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "clusters": self.clusters,
            "compacted": len(self.compacted),
            "absorbed": self.absorbed,
            "rejected": self.rejected,
        }


class CompactionEngine:
    """Compresses crowds of low-value memories into single summaries."""

    def __init__(
        self,
        repository: MemoryRepository,
        lifecycle: MemoryLifecycle,
        llm: LLMProvider,
        embeddings: EmbeddingProvider,
        settings: Settings | None = None,
    ) -> None:
        self._repository = repository
        self._lifecycle = lifecycle
        self._llm = llm
        self._embeddings = embeddings
        self._settings = settings or get_settings()

    async def run(
        self,
        scope: MemoryScope,
        *,
        now: datetime | None = None,
        dry_run: bool = False,
        config: CompactionSettings | None = None,
    ) -> CompactionOutcome:
        """Fold one user's crowd of low-value memories.

        ``config`` overrides the configured thresholds for this call only, so a
        request or a CLI flag can try a tighter cluster without changing the
        rules for every other tenant in the process.
        """
        now = now or utcnow()
        config = config or self._settings.dream.compaction
        outcome = CompactionOutcome()

        eligible = await self._eligible(scope, now, config)
        if len(eligible) < config.min_memories:
            # Not enough clutter to be worth a model call. This gate is what
            # keeps a nightly pass over a thousand users affordable.
            return outcome

        clusters = cluster_memories(
            eligible,
            similarity_threshold=config.similarity_threshold,
            min_cluster_size=config.min_cluster_size,
            max_cluster_size=config.max_cluster_size,
            max_clusters=config.max_clusters_per_run,
        )
        outcome.clusters = len(clusters)

        for cluster in clusters:
            summary = await self._summarise(cluster.memories)
            if summary is None:
                outcome.rejected += 1
                continue
            if dry_run:
                outcome.compacted.append(summary)
                outcome.absorbed += len(cluster.memories)
                continue

            canonical = await self._write(scope, summary, cluster.memories)
            outcome.compacted.append(canonical.memory_id)
            outcome.absorbed += len(cluster.memories)

        if outcome.clusters:
            logger.info(
                "compaction_finished",
                clusters=outcome.clusters,
                compacted=len(outcome.compacted),
                absorbed=outcome.absorbed,
                rejected=outcome.rejected,
                dry_run=dry_run,
            )
        return outcome

    # --- selection ------------------------------------------------------------ #

    async def _eligible(
        self, scope: MemoryScope, now: datetime, config: CompactionSettings
    ) -> list[Memory]:
        """Low-value memories that are safe to compress.

        The filters here are the safety of this whole feature. A memory that
        reaches the model is one whose loss would not matter much even if the
        summary were poor; everything that would matter is excluded before a
        single token is spent.
        """
        active = await self._repository.find(
            scope,
            statuses=[MemoryStatus.ACTIVE],
            exclude_types=[MemoryType.PATTERN.value],
            limit=config.max_candidates,
        )
        protected = {
            source_id
            for pattern in await self._repository.find_patterns(scope, only_active=True)
            for source_id in pattern.source_memory_ids
        }
        floor = utcnow() - timedelta(days=config.min_age_days)

        return [
            memory
            for memory in active
            if memory.embedding
            and memory.importance < config.importance_floor
            and memory.temporal_nature is not TemporalNature.PERMANENT
            and memory.memory_id not in protected
            # Recent memories are still doing their job. Compacting something
            # said yesterday is compressing the conversation you are in.
            and memory.created_at <= floor
            and (memory.last_recalled_at is None or memory.last_recalled_at <= floor)
        ]

    # --- the model ------------------------------------------------------------ #

    async def _summarise(self, memories: list[Memory]) -> str | None:
        payload = _dumps(
            {
                "task": "compact",
                "memories": [_memory_view(memory) for memory in memories],
            }
        )
        try:
            with operation(OP_SYNTHESIZE):
                result = await self._llm.generate_json(
                    system=COMPACTION_SYSTEM,
                    user=payload,
                    response_model=CompactionProposal,
                )
        except (LLMResponseError, ProviderError) as exc:
            logger.warning("compaction_call_failed", error=str(exc)[:200])
            return None

        proposal = result.data
        if not proposal.keep or not (proposal.summary or "").strip():
            return None

        summary = proposal.summary.strip()
        # A "summary" shorter than the shortest thing it replaces has almost
        # certainly dropped something. Cheap check, and it catches the failure
        # this feature is most likely to have.
        if len(summary) < max(len(m.content) for m in memories):
            logger.info("compaction_summary_too_short", length=len(summary))
            return None
        return summary

    async def _write(
        self, scope: MemoryScope, summary: str, absorbed: list[Memory]
    ) -> Memory:
        embedding = await self._embeddings.embed(summary)
        canonical = Memory(
            **scope.as_model_fields(),
            content=summary,
            # The absorbed memories agree on type often enough that taking the
            # most common one is better than inventing a new one.
            memory_type=max(
                {m.memory_type for m in absorbed},
                key=lambda t: sum(1 for m in absorbed if m.memory_type == t),
            ),
            importance=max(m.importance for m in absorbed),
            confidence=min(m.confidence for m in absorbed),
            # A summary is inferred, never testimony — even though every fact in
            # it was stated. The user did not say this sentence.
            explicit=False,
            embedding=embedding,
            embedding_model=self._embeddings.model_name,
            embedding_dim=len(embedding),
            metadata={"source": "compaction", "absorbed": len(absorbed)},
        )
        # `merge` activates the canonical itself, then gives it the sources and
        # provenance of everything it absorbs, leaving each original readable
        # with a pointer to it. Nothing is lost, only moved out of the active
        # set — and calling `activate` here as well is a duplicate insert.
        await self._lifecycle.merge(scope, canonical=canonical, targets=absorbed)
        return canonical
