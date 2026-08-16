"""The memory service — what the API layer actually calls.

Composes extraction, Dream reasoning, retrieval and the relationship graph into
the three operations an agent needs: remember, recall, inspect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.config import Settings, get_settings
from app.core.exceptions import InvalidStateTransitionError
from app.core.logging import get_logger
from app.dream.engine import DreamEngine, DreamResult
from app.memory.extractor import MemoryExtractor
from app.memory.lifecycle import MemoryLifecycle
from app.memory.retriever import MemoryRetriever
from app.models.dream import DreamOperation
from app.models.enums import DreamAction, MemoryStatus, RelationType
from app.models.memory import Memory, ScoredMemory, content_fingerprint, utcnow
from app.models.requests import RelatedMemory, SearchMemoryRequest
from app.models.scope import MemoryScope
from app.providers.embeddings.base import EmbeddingProvider
from app.repositories.audit_repository import AuditRepository
from app.repositories.memory_repository import MemoryRepository

logger = get_logger(__name__)


@dataclass(slots=True)
class AddOutcome:
    """The result of remembering one message, which may hold several facts."""

    results: list[DreamResult] = field(default_factory=list)
    extracted_count: int = 0
    rejected: list[tuple[str, str]] = field(default_factory=list)
    reason: str = ""

    @property
    def primary(self) -> DreamResult | None:
        """The result the flat API fields mirror.

        A consolidation is more interesting than an addition, so a message that
        both updated something and added something reports the update first.
        """
        if not self.results:
            return None
        priority = {
            DreamAction.SUPERSEDE: 0,
            DreamAction.MERGE: 1,
            DreamAction.ADD: 2,
            DreamAction.NOOP: 3,
        }
        return min(self.results, key=lambda r: priority.get(r.action, 9))


@dataclass(slots=True)
class MemoryDetail:
    memory: Memory
    relationships: list[RelatedMemory] = field(default_factory=list)
    source_memories: list[RelatedMemory] = field(default_factory=list)
    derived_memories: list[RelatedMemory] = field(default_factory=list)


class MemoryService:
    def __init__(
        self,
        *,
        repository: MemoryRepository,
        retriever: MemoryRetriever,
        lifecycle: MemoryLifecycle,
        extractor: MemoryExtractor,
        engine: DreamEngine,
        audit: AuditRepository,
        embeddings: EmbeddingProvider,
        settings: Settings | None = None,
    ) -> None:
        self._repository = repository
        self._retriever = retriever
        self._lifecycle = lifecycle
        self._extractor = extractor
        self._engine = engine
        self._audit = audit
        # Manual writes embed their own content: they never reach the extractor
        # or the Dream engine, which is where embedding normally happens.
        self._embeddings = embeddings
        self._settings = settings or get_settings()

    # --- remember ------------------------------------------------------------------ #

    async def add(
        self,
        scope: MemoryScope,
        prompt: str,
        *,
        message_id: str | None = None,
        timestamp: datetime | None = None,
        metadata: dict | None = None,
        raw: bool = False,
        request_id: str | None = None,
    ) -> AddOutcome:
        """Extract memories from a message and let Dream decide what to do.

        Candidates are processed **sequentially, not concurrently**. Each one is
        reasoned about against the state the previous one left behind — a message
        saying the same thing twice must not produce two active memories, which
        is exactly what a parallel pass would create.
        """
        if raw:
            candidates = [self._extractor.as_raw_candidate(prompt)]
            outcome = AddOutcome(extracted_count=1, reason="raw passthrough")
        else:
            extraction = await self._extractor.extract(
                prompt, now=timestamp, message_id=message_id
            )
            candidates = extraction.candidates
            outcome = AddOutcome(
                extracted_count=len(candidates),
                rejected=[(c.content, reason) for c, reason in extraction.rejected],
                reason=extraction.reason,
            )
            if not candidates:
                logger.info("no_memories_extracted", reason=extraction.reason)
                return outcome

        for candidate in candidates:
            result = await self._engine.process(
                scope,
                candidate,
                message_id=message_id,
                request_id=request_id,
                input_content=prompt,
                metadata=metadata,
                now=timestamp,
            )
            outcome.results.append(result)

        return outcome

    # --- manual management ------------------------------------------------------------ #
    #
    # The Dream layer decides what to keep from conversation. These are the
    # operator escape hatch, and they skip the reasoner entirely: the caller has
    # stated exactly what they want, so there is nothing to infer. Every one of
    # them still writes an audit row, because a human editing memory by hand is
    # more worth recording than a routine automated decision, not less.

    async def create_manual(
        self,
        scope: MemoryScope,
        *,
        content: str,
        memory_type: str = "other",
        importance: float = 0.6,
        confidence: float = 1.0,
        explicit: bool = True,
        temporal_nature: str = "current",
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        metadata: dict | None = None,
        allow_duplicate: bool = False,
        request_id: str | None = None,
    ) -> tuple[Memory, bool]:
        """Write a memory verbatim. Returns (memory, created)."""
        from app.core.metrics import OP_MANUAL, operation
        from app.models.enums import TemporalNature
        from app.models.memory import Memory as MemoryModel

        content = content.strip()

        # Silently returning the existing row beats creating a second identical
        # one; an importer re-run should converge, not multiply.
        if not allow_duplicate:
            existing = await self._repository.find_by_content_hash(
                scope, content_fingerprint(content), statuses=(MemoryStatus.ACTIVE,)
            )
            if existing is not None:
                await self._record_manual(
                    scope,
                    action="MANUAL_NOOP",
                    memory_id=existing.memory_id,
                    reason=f"an identical active memory already exists ({existing.memory_id})",
                    request_id=request_id,
                )
                return existing, False

        try:
            temporal = TemporalNature(temporal_nature)
        except ValueError:
            temporal = TemporalNature.CURRENT

        with operation(OP_MANUAL):
            embedding = await self._embeddings.embed(content)
        memory = MemoryModel(
            client_id=scope.client_id,
            bot_id=scope.bot_id,
            user_id=scope.user_id,
            phone_number_hash=scope.phone_number_hash,
            content=content,
            memory_type=(memory_type or "other").strip().lower(),
            importance=importance,
            confidence=confidence,
            explicit=explicit,
            temporal_nature=temporal,
            valid_from=valid_from or utcnow(),
            valid_until=valid_until,
            embedding=embedding,
            embedding_model=self._embeddings.model_name,
            embedding_dim=len(embedding),
            # Marked so it is distinguishable from anything the engine decided.
            metadata={**(metadata or {}), "source": "manual"},
        )

        await self._lifecycle.activate(scope, memory)
        await self._record_manual(
            scope,
            action="MANUAL_ADD",
            memory_id=memory.memory_id,
            reason="memory created manually, bypassing extraction and Dream reasoning",
            input_content=content,
            request_id=request_id,
        )
        return memory, True

    async def update_manual(
        self,
        scope: MemoryScope,
        memory_id: str,
        *,
        changes: dict,
        request_id: str | None = None,
    ) -> Memory | None:
        """Correct an active memory in place, keeping its id.

        In-place rather than supersede-and-replace: an operator fixing a bad
        extraction is correcting a *record*, not recording a change in the world.
        A supersede chain would imply the user's situation changed, which would
        be a lie in the history.

        Terminal memories are refused — history is not rewritten.
        """
        from app.models.enums import TemporalNature

        memory = await self._repository.get(scope, memory_id)
        if memory is None:
            return None

        if memory.status != MemoryStatus.ACTIVE:
            raise InvalidStateTransitionError(
                f"Cannot edit a {memory.status.value} memory. Superseded and merged "
                "memories are history and are never rewritten.",
                details={"memoryId": memory_id, "status": memory.status.value},
            )

        before = memory.content
        applied: list[str] = []

        if changes.get("content") is not None:
            new_content = changes["content"].strip()
            if new_content != memory.content:
                memory.content = new_content
                memory.content_hash = content_fingerprint(new_content)
                # The index must never disagree with the text it points at.
                from app.core.metrics import OP_MANUAL, operation

                with operation(OP_MANUAL):
                    embedding = await self._embeddings.embed(new_content)
                memory.embedding = embedding
                memory.embedding_model = self._embeddings.model_name
                memory.embedding_dim = len(embedding)
                applied.append("content")

        for name in ("importance", "confidence", "explicit"):
            if changes.get(name) is not None:
                setattr(memory, name, changes[name])
                applied.append(name)

        if changes.get("memory_type") is not None:
            memory.memory_type = changes["memory_type"].strip().lower()
            applied.append("memoryType")

        if changes.get("temporal_nature") is not None:
            try:
                memory.temporal_nature = TemporalNature(changes["temporal_nature"])
                applied.append("temporalNature")
            except ValueError:
                pass

        for name in ("valid_from", "valid_until"):
            if changes.get(name) is not None:
                setattr(memory, name, changes[name])
                applied.append(name)

        if changes.get("metadata") is not None:
            memory.metadata = {**memory.metadata, **changes["metadata"]}
            applied.append("metadata")

        memory.metadata["lastManualEdit"] = utcnow().isoformat()
        memory.touch()
        await self._lifecycle.update_in_place(scope, memory)

        await self._record_manual(
            scope,
            action="MANUAL_UPDATE",
            memory_id=memory.memory_id,
            reason=f"manually edited: {', '.join(applied) or 'no effective change'}",
            input_content=before,
            request_id=request_id,
        )
        return memory

    async def forget_memory(
        self,
        scope: MemoryScope,
        memory_id: str,
        *,
        permanent: bool = False,
        request_id: str | None = None,
    ) -> tuple[Memory | None, bool]:
        """Retire or erase one memory. Returns (memory_or_None, found)."""
        memory = await self._repository.get(scope, memory_id)
        if memory is None:
            return None, False

        if permanent:
            await self._lifecycle.destroy(scope, [memory_id])
            await self._record_manual(
                scope,
                action="DELETE",
                memory_id=memory_id,
                reason="memory permanently erased on request",
                input_content=memory.content,
                request_id=request_id,
            )
            return None, True

        updated = await self._lifecycle.forget(scope, memory)
        await self._record_manual(
            scope,
            action="EXPIRE",
            memory_id=memory_id,
            reason="memory retired on request; row retained and removed from search",
            input_content=memory.content,
            request_id=request_id,
        )
        return updated, True

    async def purge_user(self, scope: MemoryScope) -> dict[str, int]:
        """Erase everything for one user. Irreversible, and unaudited by design.

        No audit row is written: the record would outlive the data it describes,
        which defeats the purpose of an erasure request.
        """
        return await self._lifecycle.destroy_scope(scope)

    async def _record_manual(
        self,
        scope: MemoryScope,
        *,
        action: str,
        memory_id: str | None,
        reason: str,
        input_content: str | None = None,
        request_id: str | None = None,
    ) -> None:
        await self._audit.record(
            DreamOperation(
                client_id=scope.client_id,
                bot_id=scope.bot_id,
                user_id=scope.user_id,
                action=action,
                memory_id=memory_id,
                reason=reason,
                input_content=input_content,
                # No model was involved, and saying so is the point.
                model=None,
                provider="manual",
                request_id=request_id,
            )
        )

    # --- recall ---------------------------------------------------------------------- #

    async def search(
        self, scope: MemoryScope, request: SearchMemoryRequest
    ) -> list[ScoredMemory]:
        """Hybrid-ranked semantic search within one scope."""
        return await self._retriever.search(
            scope,
            request.query,
            limit=request.limit,
            latest_only=request.latest_only,
            include_superseded=request.include_superseded,
            include_merged=request.include_merged,
            include_expired=request.include_expired,
            memory_types=request.memory_types or None,
            as_of=request.as_of,
            min_score=request.min_score,
        )

    async def list_memories(
        self,
        scope: MemoryScope,
        *,
        statuses: list[MemoryStatus] | None = None,
        memory_types: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Memory], int]:
        memories = await self._repository.find(
            scope,
            statuses=statuses,
            memory_types=memory_types,
            limit=limit,
            offset=offset,
            include_embedding=False,
        )
        total = await self._repository.count(scope, statuses=statuses, memory_types=memory_types)
        return memories, total

    # --- inspect ------------------------------------------------------------------------ #

    async def get_detail(self, scope: MemoryScope, memory_id: str) -> MemoryDetail | None:
        """One memory with its graph neighbourhood resolved to readable rows."""
        memory = await self._repository.get(scope, memory_id)
        if memory is None:
            return None

        relations = await self._repository.get_relations(scope, memory_id)
        neighbour_ids = list(dict.fromkeys(r.to_memory_id for r in relations))
        # Sources may predate the edge table (they are also stored inline on the
        # memory), so union both rather than trusting either alone.
        neighbour_ids += [mid for mid in memory.source_memory_ids if mid not in neighbour_ids]

        neighbours = {
            neighbour.memory_id: neighbour
            for neighbour in await self._repository.get_many(scope, neighbour_ids)
        }

        def to_row(memory_id: str, relation_type: str) -> RelatedMemory | None:
            neighbour = neighbours.get(memory_id)
            if neighbour is None:
                return None
            return RelatedMemory(
                memory_id=neighbour.memory_id,
                relation_type=relation_type,
                content=neighbour.content,
                status=neighbour.status.value,
                memory_type=neighbour.memory_type,
            )

        relationships = [
            row
            for row in (to_row(r.to_memory_id, r.relation_type.value) for r in relations)
            if row is not None
        ]

        source_rows = [
            row
            for row in (
                to_row(source_id, RelationType.DERIVED_FROM.value)
                for source_id in memory.source_memory_ids
            )
            if row is not None
        ]

        # What rests on this memory: patterns synthesised from it, and canonical
        # memories it was merged into.
        incoming = await self._repository.get_incoming_relations(
            scope, memory_id, [RelationType.DERIVED_FROM]
        )
        derived_ids = [relation.from_memory_id for relation in incoming]
        derived_lookup = {
            derived.memory_id: derived
            for derived in await self._repository.get_many(scope, derived_ids)
        }
        derived_rows = [
            RelatedMemory(
                memory_id=derived.memory_id,
                relation_type=RelationType.SUPPORTS.value,
                content=derived.content,
                status=derived.status.value,
                memory_type=derived.memory_type,
            )
            for derived in derived_lookup.values()
        ]

        return MemoryDetail(
            memory=memory,
            relationships=relationships,
            source_memories=source_rows,
            derived_memories=derived_rows,
        )

    async def history(self, scope: MemoryScope, memory_id: str) -> list[Memory]:
        """Walk the supersede chain backwards from a memory to its origin."""
        chain: list[Memory] = []
        seen: set[str] = set()
        current = await self._repository.get(scope, memory_id)

        while current is not None and current.memory_id not in seen:
            chain.append(current)
            seen.add(current.memory_id)

            relations = await self._repository.get_relations(
                scope, current.memory_id, [RelationType.SUPERSEDES]
            )
            if not relations:
                break
            current = await self._repository.get(scope, relations[0].to_memory_id)

        return chain

    # --- stats --------------------------------------------------------------------------- #

    async def stats(self, scope: MemoryScope) -> dict:
        counts = {
            status.value: await self._repository.count(scope, statuses=[status])
            for status in MemoryStatus
        }
        tokens = await self._audit.token_usage(scope)

        # Cost is derived here rather than stored, so re-pricing is a config
        # change rather than a data migration.
        pricing = self._settings.pricing
        totals = tokens["totals"]
        totals["estimatedCost"] = round(
            pricing.cost_for(
                input_tokens=totals["inputTokens"],
                output_tokens=totals["outputTokens"],
            ),
            6,
        )
        tokens["currency"] = pricing.currency

        return {
            "counts": counts,
            "patterns": await self._repository.count(
                scope, statuses=[MemoryStatus.ACTIVE], memory_types=["pattern"]
            ),
            "actions": await self._audit.action_counts(scope),
            "tokens": tokens,
        }
