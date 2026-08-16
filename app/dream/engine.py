"""The real-time Dream decision engine.

For each extracted candidate:

    embed  ->  exact-duplicate check  ->  scoped candidate retrieval
           ->  reason (ADD / SUPERSEDE / MERGE / NOOP)
           ->  validate  ->  execute state transition  ->  audit

The cheap deterministic checks come first, so the common cases — nothing related
exists, or this is a verbatim repeat — never reach the model at all.

Two reasoning modes:

* ``unified`` (default) — one call decides among all four actions.
* ``staged`` — a merge-focused call then a supersede-focused call, each with its
  dedicated prompt. Roughly twice the tokens, more accurate on hard cases, and
  much easier to debug because each call answers one question.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from app.core.config import Settings, get_settings
from app.core.exceptions import LLMResponseError, ProviderError
from app.core.logging import get_logger, new_operation_id
from app.core.metrics import (
    DREAM_LATENCY,
    NOOP_DECISIONS,
    OP_DECIDE,
    OP_REMEMBER,
    metrics,
    operation,
)
from app.dream.merge import MergeEngine
from app.dream.prompts import DECISION_SYSTEM, build_decision_payload
from app.dream.supersede import SupersedeEngine
from app.dream.validation import ValidatedDecision, build_memory, validate_decision
from app.memory.lifecycle import MemoryLifecycle
from app.memory.retriever import MemoryRetriever
from app.models.dream import DreamDecision, DreamOperation, ExtractedMemory
from app.models.enums import (
    INHERENTLY_MUTABLE_TYPES,
    DreamAction,
    MemoryType,
    TemporalNature,
)
from app.models.memory import Memory, utcnow
from app.models.scope import MemoryScope
from app.providers.embeddings.base import EmbeddingProvider
from app.providers.llm.base import LLMProvider
from app.repositories.audit_repository import AuditRepository
from app.repositories.memory_repository import MemoryRepository

logger = get_logger(__name__)


@dataclass(slots=True)
class DreamResult:
    """The outcome of processing one candidate memory."""

    action: DreamAction
    reason: str = ""
    memory: Memory | None = None
    superseded_memory_ids: list[str] = field(default_factory=list)
    merged_memory_ids: list[str] = field(default_factory=list)
    operation_id: str = ""
    #: Every correction the backend made to the model's proposal.
    notes: list[str] = field(default_factory=list)

    @property
    def memory_id(self) -> str | None:
        return self.memory.memory_id if self.memory else None


class DreamEngine:
    def __init__(
        self,
        *,
        llm: LLMProvider,
        embeddings: EmbeddingProvider,
        repository: MemoryRepository,
        retriever: MemoryRetriever,
        lifecycle: MemoryLifecycle,
        audit: AuditRepository,
        settings: Settings | None = None,
    ) -> None:
        self._llm = llm
        self._embeddings = embeddings
        self._repository = repository
        self._retriever = retriever
        self._lifecycle = lifecycle
        self._audit = audit
        self._settings = settings or get_settings()
        self._dream = self._settings.dream

        self.supersede_engine = SupersedeEngine(llm, lifecycle, self._dream.supersede)
        self.merge_engine = MergeEngine(llm, lifecycle, repository, self._dream.merge)

    # --- public API ---------------------------------------------------------------- #

    async def process(
        self,
        scope: MemoryScope,
        candidate: ExtractedMemory,
        *,
        message_id: str | None = None,
        request_id: str | None = None,
        input_content: str | None = None,
        metadata: dict | None = None,
        now: datetime | None = None,
    ) -> DreamResult:
        """Decide and execute what should happen to one candidate memory."""
        operation_id = new_operation_id()
        now = now or utcnow()
        started = time.perf_counter()

        try:
            result = await self._process(
                scope,
                candidate,
                operation_id=operation_id,
                message_id=message_id,
                request_id=request_id,
                input_content=input_content,
                metadata=metadata,
                now=now,
            )
        finally:
            metrics.observe(DREAM_LATENCY, (time.perf_counter() - started) * 1000.0)

        return result

    # --- pipeline -------------------------------------------------------------------- #

    async def _process(
        self,
        scope: MemoryScope,
        candidate: ExtractedMemory,
        *,
        operation_id: str,
        message_id: str | None,
        request_id: str | None,
        input_content: str | None,
        metadata: dict | None,
        now: datetime,
    ) -> DreamResult:
        message_ids = [message_id] if message_id else []

        # 1. Exact duplicate — indexed hash lookup, no embedding, no LLM.
        duplicate = await self.merge_engine.find_exact_duplicate(scope, candidate.content)
        if duplicate is not None:
            return await self._noop(
                scope,
                operation_id=operation_id,
                reason=f"exact duplicate of {duplicate.memory_id}",
                targets=[duplicate.memory_id],
                request_id=request_id,
                input_content=input_content,
                candidate_ids=[duplicate.memory_id],
            )

        # 2. Embed once; reused for retrieval and, usually, for the stored memory.
        with operation(OP_REMEMBER):
            embedding = await self._embeddings.embed(candidate.content)

        # 3. Scoped candidate retrieval (vector neighbours + recent same-type).
        related = await self._retriever.find_related(
            scope,
            embedding=embedding,
            memory_type=candidate.memory_type,
            similarity_threshold=min(
                self._dream.supersede.similarity_threshold,
                self._dream.merge.similarity_threshold,
            ),
            limit=self._dream.max_related_candidates,
        )

        # 4. Nothing to compare against: this is unambiguously new.
        if not related:
            memory = build_memory(
                scope,
                proposal=candidate,
                embedding=embedding,
                embedding_model=self._embeddings.model_name,
                message_ids=message_ids,
                metadata=metadata,
            )
            await self._lifecycle.activate(scope, memory)
            await self._record(
                scope,
                operation_id=operation_id,
                action=DreamAction.ADD,
                memory_id=memory.memory_id,
                targets=[],
                reason="no related memory exists for this user",
                request_id=request_id,
                input_content=input_content,
                candidate_ids=[],
                confidence=memory.confidence,
            )
            return DreamResult(
                action=DreamAction.ADD,
                reason="no related memory exists for this user",
                memory=memory,
                operation_id=operation_id,
            )

        # 5. Reason.
        candidate_view = self._candidate_view(candidate)
        try:
            if self._dream.decision_mode == "staged":
                decision, model_name, confidence, token_usage = await self._decide_staged(
                    candidate_view, candidate, related, now
                )
            else:
                decision, model_name, confidence, token_usage = await self._decide_unified(
                    candidate_view, related, now
                )
        except (LLMResponseError, ProviderError) as exc:
            # A reasoning failure must not lose the user's information. Storing
            # it unconsolidated is recoverable; discarding it is not.
            logger.error("dream_decision_failed", error=str(exc)[:300])
            memory = build_memory(
                scope,
                proposal=candidate,
                embedding=embedding,
                embedding_model=self._embeddings.model_name,
                message_ids=message_ids,
                metadata={**(metadata or {}), "dreamFallback": "decision_failed"},
            )
            await self._lifecycle.activate(scope, memory)
            await self._record(
                scope,
                operation_id=operation_id,
                action=DreamAction.ADD,
                memory_id=memory.memory_id,
                targets=[],
                reason=f"stored without consolidation: reasoning failed ({exc})",
                request_id=request_id,
                input_content=input_content,
                candidate_ids=[m.memory_id for m, _ in related],
                notes=["dream reasoning failed; fell back to ADD"],
            )
            return DreamResult(
                action=DreamAction.ADD,
                reason="reasoning failed; stored without consolidation",
                memory=memory,
                operation_id=operation_id,
                notes=["dream reasoning failed; fell back to ADD"],
            )

        # 6. Execute.
        return await self._execute(
            scope,
            decision=decision,
            candidate=candidate,
            related=related,
            embedding=embedding,
            operation_id=operation_id,
            message_ids=message_ids,
            request_id=request_id,
            input_content=input_content,
            metadata=metadata,
            model_name=model_name,
            confidence=confidence,
            token_usage=token_usage,
        )

    # --- reasoning modes ------------------------------------------------------------ #

    def _candidate_view(self, candidate: ExtractedMemory) -> dict:
        return {
            "content": candidate.content,
            "memoryType": candidate.memory_type,
            "importance": round(candidate.importance, 3),
            "confidence": round(candidate.confidence, 3),
            "explicit": candidate.explicit,
            "temporalNature": candidate.temporal_nature.value,
            "validFrom": candidate.valid_from.isoformat() if candidate.valid_from else None,
            "validUntil": candidate.valid_until.isoformat() if candidate.valid_until else None,
        }

    async def _decide_unified(
        self, candidate_view: dict, related: list[tuple[Memory, float]], now: datetime
    ) -> tuple[ValidatedDecision, str, float | None, dict[str, int]]:
        with operation(OP_DECIDE):
            result = await self._llm.generate_json(
                system=DECISION_SYSTEM,
                user=build_decision_payload(candidate_view, related, now=now),
                response_model=DreamDecision,
            )
        validated = validate_decision(
            result.data,
            supplied_ids=[memory.memory_id for memory, _ in related],
            supersede_enabled=self.supersede_engine.enabled,
            merge_enabled=self.merge_engine.enabled,
        )
        confidence = validated.proposal.confidence if validated.proposal else None
        return validated, result.model, confidence, dict(result.usage)

    async def _decide_staged(
        self,
        candidate_view: dict,
        candidate: ExtractedMemory,
        related: list[tuple[Memory, float]],
        now: datetime,
    ) -> tuple[ValidatedDecision, str, float | None, dict[str, int]]:
        """Merge first, then supersede.

        Merge leads because it is the cheaper mistake and the narrower question:
        if this is simply the same fact restated, there is no contradiction to
        reason about and the second call is skipped entirely.
        """
        model_name = self._llm.model_name
        # Staged mode may make two calls; usage accumulates across both.
        usage: dict[str, int] = {}

        def _accumulate(result_usage: dict[str, int]) -> None:
            for key, value in result_usage.items():
                usage[key] = usage.get(key, 0) + value

        if self.merge_engine.enabled:
            merge_targets = self.merge_engine.eligible_targets(candidate.memory_type, related)
            if merge_targets:
                decision, result = await self.merge_engine.evaluate(
                    candidate_view, merge_targets, now=now
                )
                model_name = result.model
                _accumulate(result.usage)
                if decision.action == DreamAction.MERGE:
                    confidence = decision.proposal.confidence if decision.proposal else None
                    return decision, model_name, confidence, usage

        if self.supersede_engine.enabled:
            supersede_targets = self.supersede_engine.eligible_targets(
                candidate.memory_type, related
            )
            if supersede_targets:
                decision, result = await self.supersede_engine.evaluate(
                    candidate_view, supersede_targets, now=now
                )
                model_name = result.model
                _accumulate(result.usage)
                if decision.action == DreamAction.SUPERSEDE:
                    confidence = decision.proposal.confidence if decision.proposal else None
                    return decision, model_name, confidence, usage

        # Neither stage claimed it, so it is a new fact.
        return (
            ValidatedDecision(
                action=DreamAction.ADD,
                reason="neither merge nor supersede applied",
                proposal=None,
                target_ids=[],
            ),
            model_name,
            candidate.confidence,
            usage,
        )

    # --- execution ------------------------------------------------------------------- #

    async def _execute(
        self,
        scope: MemoryScope,
        *,
        decision: ValidatedDecision,
        candidate: ExtractedMemory,
        related: list[tuple[Memory, float]],
        embedding: list[float],
        operation_id: str,
        message_ids: list[str],
        request_id: str | None,
        input_content: str | None,
        metadata: dict | None,
        model_name: str,
        confidence: float | None,
        token_usage: dict[str, int] | None = None,
    ) -> DreamResult:
        by_id = {memory.memory_id: memory for memory, _ in related}
        supplied_ids = list(by_id)
        notes = list(decision.notes)

        if decision.action == DreamAction.NOOP:
            return await self._noop(
                scope,
                operation_id=operation_id,
                reason=decision.reason or "no new information",
                targets=decision.target_ids,
                request_id=request_id,
                input_content=input_content,
                candidate_ids=supplied_ids,
                model_name=model_name,
                notes=notes,
            )

        targets = [by_id[mid] for mid in decision.target_ids if mid in by_id]

        # Backend-enforced protections. The prompt asks for these; this is where
        # they actually hold.
        if decision.action == DreamAction.SUPERSEDE:
            targets, dropped = self._protect_from_supersede(targets)
            notes.extend(dropped)
            if not targets:
                notes.append("all supersede targets were protected; storing as ADD")
                decision.action = DreamAction.ADD

        if decision.action == DreamAction.MERGE:
            targets = [t for t in targets if t.memory_type != MemoryType.PATTERN.value]
            if not targets:
                notes.append("all merge targets were patterns; storing as ADD")
                decision.action = DreamAction.ADD

        proposal = decision.proposal or candidate
        memory = await self._materialize(
            scope,
            proposal=proposal,
            fallback_embedding=embedding,
            fallback_content=candidate.content,
            message_ids=message_ids,
            metadata=metadata,
        )

        if decision.action == DreamAction.SUPERSEDE:
            superseded = await self.supersede_engine.apply(
                scope,
                new_memory=memory,
                targets=targets,
                valid_until=self._supersede_boundary(decision, memory),
                operation_id=operation_id,
            )
            await self._record(
                scope,
                operation_id=operation_id,
                action=DreamAction.SUPERSEDE,
                memory_id=memory.memory_id,
                targets=superseded,
                reason=decision.reason,
                request_id=request_id,
                input_content=input_content,
                candidate_ids=supplied_ids,
                model_name=model_name,
                confidence=confidence,
                notes=notes,
                token_usage=token_usage,
            )
            return DreamResult(
                action=DreamAction.SUPERSEDE,
                reason=decision.reason,
                memory=memory,
                superseded_memory_ids=superseded,
                operation_id=operation_id,
                notes=notes,
            )

        if decision.action == DreamAction.MERGE:
            notes.extend(
                self.merge_engine.check_no_information_lost(
                    memory.content, targets, candidate.content
                )
            )
            merged = await self.merge_engine.apply(
                scope, canonical=memory, targets=targets, operation_id=operation_id
            )
            await self._record(
                scope,
                operation_id=operation_id,
                action=DreamAction.MERGE,
                memory_id=memory.memory_id,
                targets=merged,
                reason=decision.reason,
                request_id=request_id,
                input_content=input_content,
                candidate_ids=supplied_ids,
                model_name=model_name,
                confidence=confidence,
                notes=notes,
                token_usage=token_usage,
            )
            return DreamResult(
                action=DreamAction.MERGE,
                reason=decision.reason,
                memory=memory,
                merged_memory_ids=merged,
                operation_id=operation_id,
                notes=notes,
            )

        # ADD
        await self._lifecycle.activate(scope, memory)
        await self._record(
            scope,
            operation_id=operation_id,
            action=DreamAction.ADD,
            memory_id=memory.memory_id,
            targets=[],
            reason=decision.reason or "new fact",
            request_id=request_id,
            input_content=input_content,
            candidate_ids=supplied_ids,
            model_name=model_name,
            confidence=confidence,
            notes=notes,
            token_usage=token_usage,
        )
        return DreamResult(
            action=DreamAction.ADD,
            reason=decision.reason or "new fact",
            memory=memory,
            operation_id=operation_id,
            notes=notes,
        )

    @staticmethod
    def _protect_from_supersede(targets: list[Memory]) -> tuple[list[Memory], list[str]]:
        """Refuse to retire memories that must not be retired this way.

        Patterns are re-evaluated by synthesis, not replaced by raw facts.
        Permanent facts — birthplace, date of birth — do not stop being true, so
        a later "I moved to Bangalore" must not retire them. Both are enforced
        here rather than left to the prompt, because a single bad decision would
        otherwise destroy the unrecoverable answer to a question.

        The permanence guard is deliberately *not* absolute. Some memory types
        are mutable by their nature: a preference, a habit, a goal, a job can
        always be reversed by the person whose preference it is. Honouring a
        `permanent` label on those would mean one mislabel at extraction time
        freezes a memory forever, with no way to correct it through
        conversation — turning a safety check into a trap.
        """
        kept: list[Memory] = []
        notes: list[str] = []

        for memory in targets:
            if memory.memory_type == MemoryType.PATTERN.value:
                notes.append(f"refused to supersede pattern {memory.memory_id}")
                continue

            if memory.temporal_nature == TemporalNature.PERMANENT:
                if memory.memory_type in INHERENTLY_MUTABLE_TYPES:
                    notes.append(
                        f"ignored the 'permanent' label on {memory.memory_id}: a "
                        f"{memory.memory_type} is mutable by nature"
                    )
                else:
                    notes.append(f"refused to supersede permanent fact {memory.memory_id}")
                    continue

            kept.append(memory)

        return kept, notes

    @staticmethod
    def _supersede_boundary(decision: ValidatedDecision, memory: Memory) -> datetime | None:
        """When the superseded facts stopped being true."""
        return memory.valid_from or memory.created_at

    async def _materialize(
        self,
        scope: MemoryScope,
        *,
        proposal,
        fallback_embedding: list[float],
        fallback_content: str,
        message_ids: list[str],
        metadata: dict | None,
    ) -> Memory:
        """Build the memory to persist, re-embedding only if the text changed.

        Merge and supersede often rewrite the content into canonical form, and
        an index entry that does not match its own text is a subtle retrieval
        bug — so the check is on the exact string.
        """
        embedding = fallback_embedding
        if proposal.content.strip() != fallback_content.strip():
            with operation(OP_REMEMBER):
                embedding = await self._embeddings.embed(proposal.content)

        return build_memory(
            scope,
            proposal=proposal,
            embedding=embedding,
            embedding_model=self._embeddings.model_name,
            message_ids=message_ids,
            metadata=metadata,
        )

    # --- audit ------------------------------------------------------------------------- #

    async def _noop(
        self,
        scope: MemoryScope,
        *,
        operation_id: str,
        reason: str,
        targets: list[str],
        request_id: str | None,
        input_content: str | None,
        candidate_ids: list[str],
        model_name: str | None = None,
        notes: list[str] | None = None,
    ) -> DreamResult:
        metrics.increment(NOOP_DECISIONS)
        await self._record(
            scope,
            operation_id=operation_id,
            action=DreamAction.NOOP,
            memory_id=None,
            targets=targets,
            reason=reason,
            request_id=request_id,
            input_content=input_content,
            candidate_ids=candidate_ids,
            model_name=model_name,
            notes=notes,
        )
        return DreamResult(
            action=DreamAction.NOOP,
            reason=reason,
            operation_id=operation_id,
            notes=list(notes or []),
        )

    async def _record(
        self,
        scope: MemoryScope,
        *,
        operation_id: str,
        action: DreamAction,
        memory_id: str | None,
        targets: list[str],
        reason: str,
        request_id: str | None,
        input_content: str | None,
        candidate_ids: list[str],
        model_name: str | None = None,
        confidence: float | None = None,
        notes: list[str] | None = None,
        job_id: str | None = None,
        token_usage: dict[str, int] | None = None,
    ) -> None:
        await self._audit.record(
            DreamOperation(
                operation_id=operation_id,
                client_id=scope.client_id,
                bot_id=scope.bot_id,
                user_id=scope.user_id,
                action=action.value,
                memory_id=memory_id,
                target_memory_ids=list(targets),
                reason=reason,
                input_content=input_content,
                candidate_memory_ids=list(candidate_ids),
                model=model_name or self._llm.model_name,
                provider=self._settings.llm_provider,
                confidence=confidence,
                request_id=request_id,
                job_id=job_id,
                validation_notes=list(notes or []),
                # Per-decision cost, so token spend is attributable to the
                # exact operation that incurred it rather than only in aggregate.
                token_usage=dict(token_usage or {}),
            )
        )
