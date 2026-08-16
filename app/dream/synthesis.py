"""The synthesis engine.

Runs in the background, never on a user's request path:

    select candidates -> cluster -> per cluster: reason -> validate
                      -> idempotency check -> persist pattern + provenance

Idempotency is the hard requirement and gets two independent checks, because the
failure mode is quietly catastrophic: run synthesis nightly without them and a
user accumulates fifty near-identical "User prefers morning meetings" patterns
that poison every subsequent retrieval.

    1. **Source overlap** — deterministic. If an existing pattern already rests
       on substantially the same evidence, this cluster has been synthesised
       before, whatever words the model chose this time.
    2. **Semantic similarity** — embedding-based. Catches the same idea derived
       from a *different* subset of evidence, which the first check cannot see.

Either check firing means: update the existing pattern if the new evidence adds
something, otherwise skip. Never create.

Source memories are never modified. Synthesis is purely additive.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.core.config import Settings, get_settings
from app.core.exceptions import LLMResponseError, ProviderError
from app.core.logging import get_logger, new_operation_id
from app.core.metrics import (
    OP_SYNTHESIZE,
    PATTERNS_CREATED,
    PATTERNS_UPDATED,
    SYNTHESIS_DURATION,
    SYNTHESIS_RUNS,
    metrics,
    operation,
)
from app.dream.candidate_selector import CandidateSelector
from app.dream.clustering import Cluster, cluster_memories
from app.dream.prompts import SYNTHESIS_SYSTEM, build_synthesis_payload
from app.memory.lifecycle import MemoryLifecycle
from app.models.dream import DreamJob, DreamOperation, PatternProposal, SynthesisDecision
from app.models.enums import JobStatus, MemoryStatus, MemoryType, RelationType, SynthesisAction
from app.models.memory import Memory, new_memory_id, utcnow
from app.models.scope import MemoryScope
from app.providers.embeddings.base import EmbeddingProvider, cosine_similarity
from app.providers.llm.base import LLMProvider
from app.repositories.audit_repository import AuditRepository
from app.repositories.memory_repository import MemoryRepository

logger = get_logger(__name__)


@dataclass(slots=True)
class ClusterOutcome:
    action: str
    reason: str = ""
    pattern_id: str | None = None
    notes: list[str] = field(default_factory=list)


class SynthesisEngine:
    def __init__(
        self,
        *,
        llm: LLMProvider,
        embeddings: EmbeddingProvider,
        repository: MemoryRepository,
        lifecycle: MemoryLifecycle,
        audit: AuditRepository,
        settings: Settings | None = None,
    ) -> None:
        self._llm = llm
        self._embeddings = embeddings
        self._repository = repository
        self._lifecycle = lifecycle
        self._audit = audit
        self._settings = settings or get_settings()
        self._config = self._settings.dream.synthesis
        self._selector = CandidateSelector(repository, self._config)

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    # --- entry point -------------------------------------------------------------- #

    async def run(self, scope: MemoryScope, job: DreamJob, *, force: bool = False) -> DreamJob:
        """Execute one synthesis job, updating and returning the job record."""
        started = time.perf_counter()
        metrics.increment(SYNTHESIS_RUNS)
        job.started_at = utcnow()

        if not self._config.enabled:
            job.status = JobStatus.SKIPPED
            job.error = "synthesis is disabled by configuration"
            return job

        try:
            candidates = await self._selector.select(scope)
            job.memories_analyzed = len(candidates.memories)

            if not candidates.has_enough:
                job.status = JobStatus.SKIPPED
                job.error = (
                    f"user has {candidates.total_active} active memories; "
                    f"{self._config.min_memories} required"
                )
                return job

            clusters = cluster_memories(
                candidates.memories,
                similarity_threshold=self._config.cluster_similarity_threshold,
                min_cluster_size=self._config.min_cluster_size,
                max_cluster_size=self._config.max_cluster_size,
                max_clusters=self._config.max_clusters_per_run,
            )
            job.clusters_evaluated = len(clusters)

            if not clusters:
                job.status = JobStatus.COMPLETED
                job.error = None
                return job

            # Re-read patterns between clusters: a pattern created for cluster 1
            # must be visible to the duplicate check for cluster 2, or a single
            # run can emit two versions of the same idea.
            known_patterns = list(candidates.existing_patterns)

            for cluster in clusters:
                outcome = await self._process_cluster(
                    scope, cluster, known_patterns, job=job, force=force
                )
                if outcome.action == "created":
                    job.patterns_created += 1
                    if outcome.pattern_id:
                        job.created_pattern_ids.append(outcome.pattern_id)
                        refreshed = await self._repository.get(scope, outcome.pattern_id)
                        if refreshed:
                            known_patterns.append(refreshed)
                elif outcome.action == "updated":
                    job.patterns_updated += 1
                    known_patterns = await self._repository.find_patterns(scope, only_active=True)
                elif outcome.action == "skipped":
                    job.patterns_skipped += 1
                else:
                    job.noop_decisions += 1

            job.status = JobStatus.COMPLETED
            job.error = None

        except Exception as exc:  # noqa: BLE001 - the worker needs the reason on the job
            logger.exception("synthesis_failed", error=str(exc)[:300])
            job.status = JobStatus.FAILED
            job.error = f"{type(exc).__name__}: {exc}"[:2000]

        finally:
            duration = (time.perf_counter() - started) * 1000.0
            metrics.observe(SYNTHESIS_DURATION, duration)
            job.duration_ms = duration

        return job

    # --- per cluster ------------------------------------------------------------------ #

    async def _process_cluster(
        self,
        scope: MemoryScope,
        cluster: Cluster,
        known_patterns: list[Memory],
        *,
        job: DreamJob,
        force: bool,
    ) -> ClusterOutcome:
        operation_id = new_operation_id()
        cluster_ids = set(cluster.memory_ids)

        # Cheap pre-check before the model call: if an existing pattern already
        # rests on this evidence, there is nothing to ask.
        if not force:
            existing = self._find_by_source_overlap(cluster_ids, known_patterns)
            if existing is not None:
                await self._record(
                    scope,
                    operation_id=operation_id,
                    action="NOOP",
                    memory_id=existing.memory_id,
                    targets=cluster.memory_ids,
                    reason=(
                        f"pattern {existing.memory_id} already covers this evidence "
                        "(source overlap)"
                    ),
                    job_id=job.job_id,
                )
                return ClusterOutcome("skipped", "source overlap with an existing pattern")

        try:
            with operation(OP_SYNTHESIZE):
                result = await self._llm.generate_json(
                    system=SYNTHESIS_SYSTEM,
                    user=build_synthesis_payload(cluster.memories, known_patterns),
                    response_model=SynthesisDecision,
                )
        except (LLMResponseError, ProviderError) as exc:
            # One cluster failing must not fail the whole run.
            logger.error("synthesis_cluster_failed", error=str(exc)[:300])
            return ClusterOutcome("noop", f"reasoning failed: {exc}")

        decision = result.data
        if decision.action != SynthesisAction.SYNTHESIZE or decision.pattern is None:
            await self._record(
                scope,
                operation_id=operation_id,
                action="NOOP",
                memory_id=None,
                targets=cluster.memory_ids,
                reason=decision.reason or "no pattern in this cluster",
                model=result.model,
                job_id=job.job_id,
            )
            return ClusterOutcome("noop", decision.reason)

        return await self._persist_pattern(
            scope,
            proposal=decision.pattern,
            cluster=cluster,
            known_patterns=known_patterns,
            operation_id=operation_id,
            model=result.model,
            reason=decision.reason,
            job=job,
            force=force,
        )

    # --- validation and persistence --------------------------------------------------- #

    async def _persist_pattern(
        self,
        scope: MemoryScope,
        *,
        proposal: PatternProposal,
        cluster: Cluster,
        known_patterns: list[Memory],
        operation_id: str,
        model: str,
        reason: str,
        job: DreamJob,
        force: bool,
    ) -> ClusterOutcome:
        notes: list[str] = []
        cluster_ids = set(cluster.memory_ids)

        # Sources must come from this cluster. An id from anywhere else is either
        # a hallucination or an attempt to reference a memory the model was not
        # shown, and neither may become provenance.
        sources = [mid for mid in dict.fromkeys(proposal.source_memory_ids) if mid in cluster_ids]
        invented = [mid for mid in proposal.source_memory_ids if mid not in cluster_ids]
        if invented:
            notes.append(f"dropped {len(invented)} source id(s) not in the cluster")
            logger.warning("synthesis_invented_source_ids", ids=invented)

        content = proposal.content.strip()
        if not content:
            return ClusterOutcome("noop", "empty pattern content")

        # Traceability is not optional: a pattern nobody can audit back to
        # evidence is worse than no pattern.
        if len(sources) < self._config.min_cluster_size:
            notes.append(
                f"only {len(sources)} valid source ids; "
                f"{self._config.min_cluster_size} required"
            )
            await self._record(
                scope,
                operation_id=operation_id,
                action="NOOP",
                memory_id=None,
                targets=cluster.memory_ids,
                reason="insufficient traceable evidence",
                model=model,
                notes=notes,
                job_id=job.job_id,
            )
            return ClusterOutcome("noop", "insufficient traceable evidence", notes=notes)

        if (
            proposal.confidence < self._config.min_pattern_confidence
            or proposal.importance < self._config.min_pattern_importance
        ):
            await self._record(
                scope,
                operation_id=operation_id,
                action="NOOP",
                memory_id=None,
                targets=sources,
                reason=(
                    f"pattern below quality bar "
                    f"(confidence {proposal.confidence:.2f}, importance {proposal.importance:.2f})"
                ),
                model=model,
                job_id=job.job_id,
            )
            return ClusterOutcome("noop", "below the configured quality bar")

        with operation(OP_SYNTHESIZE):
            embedding = await self._embeddings.embed(content)

        # Second idempotency check: same idea, possibly different evidence.
        duplicate = self._find_semantic_duplicate(embedding, known_patterns)
        if duplicate is None and not force:
            duplicate = self._find_by_source_overlap(set(sources), known_patterns)

        if duplicate is not None:
            return await self._reconcile_existing(
                scope,
                existing=duplicate,
                sources=sources,
                content=content,
                embedding=embedding,
                proposal=proposal,
                operation_id=operation_id,
                model=model,
                job=job,
                notes=notes,
            )

        pattern = Memory(
            memory_id=new_memory_id(MemoryType.PATTERN.value),
            client_id=scope.client_id,
            bot_id=scope.bot_id,
            user_id=scope.user_id,
            phone_number_hash=scope.phone_number_hash,
            content=content,
            memory_type=MemoryType.PATTERN.value,
            status=MemoryStatus.ACTIVE,
            importance=proposal.importance,
            confidence=proposal.confidence,
            # A pattern is inferred, never testimony. Downstream consumers must
            # be able to tell the difference.
            explicit=False,
            source_memory_ids=sources,
            embedding=embedding,
            embedding_model=self._embeddings.model_name,
            embedding_dim=len(embedding),
            last_synthesized_at=utcnow(),
            metadata={
                "clusterCohesion": round(cluster.cohesion, 4),
                "clusterSize": cluster.size,
                "jobId": job.job_id,
            },
        )

        await self._lifecycle.activate(scope, pattern)
        await self._repository.add_relations(
            scope,
            pattern.memory_id,
            sources,
            RelationType.DERIVED_FROM,
            operation_id=operation_id,
        )

        metrics.increment(PATTERNS_CREATED)
        await self._record(
            scope,
            operation_id=operation_id,
            action="SYNTHESIZE",
            memory_id=pattern.memory_id,
            targets=sources,
            reason=reason or "recurring pattern across multiple memories",
            model=model,
            confidence=proposal.confidence,
            notes=notes,
            job_id=job.job_id,
        )
        logger.info(
            "pattern_created",
            pattern_id=pattern.memory_id,
            sources=len(sources),
            cohesion=round(cluster.cohesion, 3),
        )
        return ClusterOutcome("created", reason, pattern.memory_id, notes)

    async def _reconcile_existing(
        self,
        scope: MemoryScope,
        *,
        existing: Memory,
        sources: list[str],
        content: str,
        embedding: list[float],
        proposal: PatternProposal,
        operation_id: str,
        model: str,
        job: DreamJob,
        notes: list[str],
    ) -> ClusterOutcome:
        """Fold a re-derived pattern into the one that already exists.

        Updating in place — rather than creating a second pattern, or replacing
        the first — is what makes repeated runs converge. The pattern keeps its
        id, so anything referencing it stays valid.
        """
        new_sources = [mid for mid in sources if mid not in existing.source_memory_ids]

        if not new_sources and not existing.stale:
            await self._record(
                scope,
                operation_id=operation_id,
                action="NOOP",
                memory_id=existing.memory_id,
                targets=sources,
                reason=f"equivalent pattern {existing.memory_id} already exists",
                model=model,
                notes=notes,
                job_id=job.job_id,
            )
            return ClusterOutcome("skipped", "equivalent pattern already exists", existing.memory_id)

        existing.source_memory_ids = list(
            dict.fromkeys([*existing.source_memory_ids, *new_sources])
        )
        # More evidence for the same conclusion raises confidence, with a ceiling:
        # an inferred pattern should never claim the certainty of testimony.
        existing.confidence = min(0.97, max(existing.confidence, proposal.confidence))
        existing.importance = max(existing.importance, proposal.importance)
        existing.last_synthesized_at = utcnow()
        existing.stale = False

        # Only rewrite the text when the new phrasing genuinely says more.
        if len(content) > len(existing.content) * 1.15:
            existing.content = content
            existing.embedding = embedding
            existing.embedding_dim = len(embedding)
            notes.append("pattern text replaced with a more detailed formulation")

        await self._lifecycle.update_in_place(scope, existing)
        await self._repository.add_relations(
            scope,
            existing.memory_id,
            new_sources,
            RelationType.DERIVED_FROM,
            operation_id=operation_id,
        )

        metrics.increment(PATTERNS_UPDATED)
        await self._record(
            scope,
            operation_id=operation_id,
            action="SYNTHESIZE_UPDATE",
            memory_id=existing.memory_id,
            targets=new_sources or sources,
            reason=f"reinforced existing pattern with {len(new_sources)} new source(s)",
            model=model,
            confidence=existing.confidence,
            notes=notes,
            job_id=job.job_id,
        )
        logger.info(
            "pattern_updated",
            pattern_id=existing.memory_id,
            new_sources=len(new_sources),
        )
        return ClusterOutcome("updated", "reinforced an existing pattern", existing.memory_id, notes)

    # --- duplicate detection ------------------------------------------------------------- #

    def _find_by_source_overlap(
        self, source_ids: set[str], patterns: list[Memory]
    ) -> Memory | None:
        """A pattern already resting on substantially the same evidence.

        Overlap is measured against the smaller of the two source sets, so a
        broad pattern absorbing a narrow one still counts as covering it.
        """
        if not source_ids:
            return None

        for pattern in patterns:
            existing = set(pattern.source_memory_ids)
            if not existing:
                continue
            denominator = min(len(existing), len(source_ids))
            if denominator == 0:
                continue
            overlap = len(existing & source_ids) / denominator
            if overlap >= self._config.duplicate_source_overlap:
                return pattern
        return None

    def _find_semantic_duplicate(
        self, embedding: list[float], patterns: list[Memory]
    ) -> Memory | None:
        """A pattern that already says this, whatever evidence it came from."""
        best: Memory | None = None
        best_score = self._config.duplicate_similarity_threshold

        for pattern in patterns:
            if not pattern.embedding:
                continue
            score = cosine_similarity(embedding, pattern.embedding)
            if score >= best_score:
                best_score = score
                best = pattern
        return best

    # --- audit ----------------------------------------------------------------------------- #

    async def _record(
        self,
        scope: MemoryScope,
        *,
        operation_id: str,
        action: str,
        memory_id: str | None,
        targets: list[str],
        reason: str,
        model: str | None = None,
        confidence: float | None = None,
        notes: list[str] | None = None,
        job_id: str | None = None,
    ) -> None:
        await self._audit.record(
            DreamOperation(
                operation_id=operation_id,
                client_id=scope.client_id,
                bot_id=scope.bot_id,
                user_id=scope.user_id,
                action=action,
                memory_id=memory_id,
                target_memory_ids=list(targets),
                reason=reason,
                candidate_memory_ids=list(targets),
                model=model or self._llm.model_name,
                provider=self._settings.llm_provider,
                confidence=confidence,
                job_id=job_id,
                validation_notes=list(notes or []),
            )
        )
