"""Memory lifecycle — the only code allowed to change a memory's status.

    NEW -> ACTIVE -> SUPERSEDED
                  -> MERGED
                  -> EXPIRED

Transitions out of a terminal state are refused. Every transition also keeps the
three derived stores consistent, which is the real reason this lives in one
place rather than inside each Dream engine:

    canonical row  ->  relationship edges  ->  vector index  ->  pattern staleness

Nothing here deletes a memory. `SUPERSEDED` and `MERGED` rows are retained in
full, with pointers to whatever replaced them.
"""

from __future__ import annotations

from datetime import datetime

from app.core.exceptions import InvalidStateTransitionError
from app.core.logging import get_logger
from app.core.metrics import MEMORIES_ADDED, MEMORIES_MERGED, MEMORIES_SUPERSEDED, metrics
from app.memory.retriever import MemoryRetriever
from app.models.enums import MemoryStatus, RelationType
from app.models.memory import Memory, utcnow
from app.models.scope import MemoryScope
from app.repositories.memory_repository import MemoryRepository

logger = get_logger(__name__)

#: Legal status changes. Terminal states have no outgoing edges: history is
#: immutable, so a superseded memory can never be re-superseded or revived.
_ALLOWED_TRANSITIONS: dict[MemoryStatus, frozenset[MemoryStatus]] = {
    MemoryStatus.ACTIVE: frozenset(
        {MemoryStatus.SUPERSEDED, MemoryStatus.MERGED, MemoryStatus.EXPIRED}
    ),
    MemoryStatus.SUPERSEDED: frozenset(),
    MemoryStatus.MERGED: frozenset(),
    MemoryStatus.EXPIRED: frozenset(),
}


class MemoryLifecycle:
    def __init__(self, repository: MemoryRepository, retriever: MemoryRetriever) -> None:
        self._repository = repository
        self._retriever = retriever

    # --- validation ---------------------------------------------------------- #

    @staticmethod
    def can_transition(current: MemoryStatus, target: MemoryStatus) -> bool:
        return target in _ALLOWED_TRANSITIONS.get(current, frozenset())

    @staticmethod
    def assert_transition(memory: Memory, target: MemoryStatus) -> None:
        if not MemoryLifecycle.can_transition(memory.status, target):
            raise InvalidStateTransitionError(
                f"Cannot move {memory.memory_id} from {memory.status.value} to {target.value}",
                details={"memoryId": memory.memory_id, "current": memory.status.value},
            )

    def filter_transitionable(
        self, memories: list[Memory], target: MemoryStatus
    ) -> tuple[list[Memory], list[str]]:
        """Split a target list into what can move and what is already terminal.

        Used instead of `assert_transition` on Dream paths: a replayed decision
        naming an already-superseded memory should converge quietly, not error.
        """
        movable: list[Memory] = []
        skipped: list[str] = []
        for memory in memories:
            if self.can_transition(memory.status, target):
                movable.append(memory)
            else:
                skipped.append(memory.memory_id)
        return movable, skipped

    # --- transitions ---------------------------------------------------------- #

    async def activate(self, scope: MemoryScope, memory: Memory) -> Memory:
        """Persist a brand-new active memory and index it."""
        memory.status = MemoryStatus.ACTIVE
        await self._repository.insert(memory)
        await self._retriever.index_memory(scope, memory)
        metrics.increment(MEMORIES_ADDED, labels={"type": memory.memory_type})
        logger.info(
            "memory_activated",
            memory_id=memory.memory_id,
            memory_type=memory.memory_type,
            importance=round(memory.importance, 3),
        )
        return memory

    async def supersede(
        self,
        scope: MemoryScope,
        *,
        new_memory: Memory,
        targets: list[Memory],
        valid_until: datetime | None = None,
        operation_id: str | None = None,
    ) -> list[str]:
        """Activate ``new_memory`` and retire the memories it replaces.

        The new memory is written *first*. If the process dies between the two
        writes, the outcome is a duplicate active memory — recoverable, and
        visible to the next Dream pass. The reverse order would leave a user with
        neither the old fact nor the new one.
        """
        movable, skipped = self.filter_transitionable(targets, MemoryStatus.SUPERSEDED)
        if skipped:
            logger.info("supersede_skipped_terminal", memory_ids=skipped)

        # Close the old facts' validity window where the new one opens.
        boundary = valid_until or new_memory.valid_from or new_memory.created_at
        new_memory.source_memory_ids = list(
            dict.fromkeys([*new_memory.source_memory_ids, *(m.memory_id for m in movable)])
        )
        await self.activate(scope, new_memory)

        target_ids = [memory.memory_id for memory in movable]
        superseded_ids = await self._repository.mark_superseded(
            scope,
            target_ids,
            superseded_by=new_memory.memory_id,
            valid_until=boundary,
            operation_id=operation_id,
        )

        await self._repository.add_relations(
            scope,
            new_memory.memory_id,
            superseded_ids,
            RelationType.SUPERSEDES,
            operation_id=operation_id,
        )

        # Superseded memories stay in the index: "where did the user live before?"
        # is a question this system exists to answer.
        await self._flag_dependent_patterns(scope, superseded_ids)

        metrics.increment(MEMORIES_SUPERSEDED, float(len(superseded_ids)))
        logger.info(
            "memories_superseded",
            new_memory_id=new_memory.memory_id,
            superseded=superseded_ids,
            valid_until=boundary.isoformat() if boundary else None,
        )
        return superseded_ids

    async def merge(
        self,
        scope: MemoryScope,
        *,
        canonical: Memory,
        targets: list[Memory],
        operation_id: str | None = None,
    ) -> list[str]:
        """Fold ``targets`` into a canonical memory.

        The canonical memory inherits the sources and provenance of everything it
        absorbs, so no traceability is lost when the originals leave the active
        set.
        """
        movable, skipped = self.filter_transitionable(targets, MemoryStatus.MERGED)
        if skipped:
            logger.info("merge_skipped_terminal", memory_ids=skipped)

        canonical.source_memory_ids = list(
            dict.fromkeys(
                [
                    *canonical.source_memory_ids,
                    *(m.memory_id for m in movable),
                    # Carry forward what the absorbed memories themselves came from.
                    *(sid for m in movable for sid in m.source_memory_ids),
                ]
            )
        )
        canonical.source_message_ids = list(
            dict.fromkeys(
                [
                    *canonical.source_message_ids,
                    *(mid for m in movable for mid in m.source_message_ids),
                ]
            )
        )
        # The merged fact is as old as the oldest thing it absorbed: a merge is a
        # rewording, not a rediscovery, and retrieval recency should reflect that.
        for memory in movable:
            if memory.valid_from and (
                canonical.valid_from is None or memory.valid_from < canonical.valid_from
            ):
                canonical.valid_from = memory.valid_from
            canonical.importance = max(canonical.importance, memory.importance)

        await self.activate(scope, canonical)

        target_ids = [memory.memory_id for memory in movable]
        merged_ids = await self._repository.mark_merged(
            scope, target_ids, merged_into=canonical.memory_id, operation_id=operation_id
        )

        await self._repository.add_relations(
            scope,
            canonical.memory_id,
            merged_ids,
            RelationType.MERGED_FROM,
            operation_id=operation_id,
        )

        # Unlike supersede, merged memories leave the index. Their content is now
        # fully contained in the canonical memory, so keeping them searchable
        # would return the same fact twice under two ids.
        await self._retriever.unindex(scope, merged_ids)
        await self._flag_dependent_patterns(scope, merged_ids)

        metrics.increment(MEMORIES_MERGED, float(len(merged_ids)))
        logger.info(
            "memories_merged", canonical_id=canonical.memory_id, merged=merged_ids
        )
        return merged_ids

    async def expire(
        self,
        scope: MemoryScope,
        targets: list[Memory],
        *,
        valid_until: datetime | None = None,
    ) -> list[str]:
        """Retire memories whose validity window has closed with no replacement."""
        movable, _ = self.filter_transitionable(targets, MemoryStatus.EXPIRED)
        expired_ids = await self._repository.mark_expired(
            scope, [memory.memory_id for memory in movable], valid_until=valid_until
        )
        await self._retriever.unindex(scope, expired_ids)
        await self._flag_dependent_patterns(scope, expired_ids)
        return expired_ids

    async def update_in_place(
        self, scope: MemoryScope, memory: Memory, *, reindex: bool = True
    ) -> Memory:
        """Rewrite a memory without changing its status.

        Used by synthesis to refresh a pattern whose evidence has grown, and by
        operator edits correcting a bad extraction.
        """
        await self._repository.replace(scope, memory)
        if reindex:
            await self._retriever.index_memory(scope, memory)
        return memory

    # --- destructive operator actions ------------------------------------------ #
    #
    # Deliberately separated from everything above. No Dream operation can reach
    # these: they are only invoked by an explicit operator or end-user request.

    async def forget(self, scope: MemoryScope, memory: Memory) -> Memory | None:
        """Retire a memory on request — the reversible kind of "delete".

        The row is kept and leaves the search index, so the memory stops
        influencing answers but remains auditable. This is what "forget that"
        should do: a wrong extraction, or a user retracting something.

        Already-terminal memories are returned unchanged rather than erroring,
        so repeating the request converges instead of failing.
        """
        if memory.status != MemoryStatus.ACTIVE:
            logger.info(
                "forget_skipped_terminal",
                memory_id=memory.memory_id,
                status=memory.status.value,
            )
            return memory

        expired = await self._repository.mark_expired(
            scope, [memory.memory_id], valid_until=utcnow()
        )
        if not expired:
            return None

        await self._retriever.unindex(scope, [memory.memory_id])
        await self._flag_dependent_patterns(scope, expired)

        logger.info("memory_forgotten", memory_id=memory.memory_id)
        return await self._repository.get(scope, memory.memory_id)

    async def destroy(self, scope: MemoryScope, memory_ids: list[str]) -> int:
        """Permanently erase memories. Irreversible.

        Distinct from `forget` on purpose. "Stop using this" and "erase this"
        carry very different risk, and giving them one verb is how data gets
        lost by accident.
        """
        if not memory_ids:
            return 0

        # Unindex first: an index entry pointing at a deleted row would return a
        # hit that cannot be resolved.
        await self._retriever.unindex(scope, memory_ids)
        deleted = await self._repository.delete_memories(scope, memory_ids)
        await self._flag_dependent_patterns(scope, memory_ids)
        return deleted

    async def destroy_scope(self, scope: MemoryScope) -> dict[str, int]:
        """Erase everything for one user, including their audit trail.

        The erasure path. The vector partition is dropped wholesale rather than
        entry by entry, so nothing survives in the index.
        """
        try:
            await self._retriever.clear_index(scope)
        except NotImplementedError:  # pragma: no cover - store without clear()
            logger.warning("vector_clear_unsupported", scope=scope.partition_key)

        counts = await self._repository.delete_scope(scope)
        logger.warning("scope_erased", **counts)
        return counts

    # --- pattern maintenance -------------------------------------------------- #

    async def _flag_dependent_patterns(
        self, scope: MemoryScope, changed_memory_ids: list[str]
    ) -> None:
        """Mark patterns whose evidence just changed as needing re-evaluation.

        Deliberately a flag and not a deletion. A pattern built on four memories
        does not become false because one was reworded, and quietly deleting
        derived knowledge on every edit would make the layer unstable. The next
        synthesis run decides.
        """
        if not changed_memory_ids:
            return
        patterns = await self._repository.find_patterns_referencing(scope, changed_memory_ids)
        if not patterns:
            return
        pattern_ids = [pattern.memory_id for pattern in patterns]
        await self._repository.mark_stale(scope, pattern_ids, stale=True)
        logger.info(
            "patterns_flagged_stale",
            pattern_ids=pattern_ids,
            changed_memories=changed_memory_ids,
        )
