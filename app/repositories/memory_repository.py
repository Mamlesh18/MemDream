"""Persistence for memories and the edges between them.

Two rules hold for every method here:

1. **Scope first.** Every filter starts from ``scope.as_filter()``. There is no
   method that reads or writes a memory without a `MemoryScope`, so a caller
   cannot accidentally query across tenants — and every document that comes back
   is re-checked against the scope before it is returned.

2. **No destructive deletes.** Status transitions are the only way a memory
   leaves the active set. `delete_*` exists solely for tests and off-boarding.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pymongo import DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.exceptions import InvalidStateTransitionError, TenantIsolationError
from app.core.logging import get_logger
from app.models.enums import MemoryStatus, MemoryType, RelationType
from app.models.memory import Memory, MemoryRelation, utcnow
from app.models.scope import MemoryScope
from app.repositories.mongo import MongoDatabase

logger = get_logger(__name__)


class MemoryRepository:
    def __init__(self, database: MongoDatabase) -> None:
        self._db = database

    # --- helpers ------------------------------------------------------------- #

    @staticmethod
    def _statuses(statuses: list[MemoryStatus] | tuple[MemoryStatus, ...] | None) -> Any:
        if not statuses:
            return None
        return {"$in": [s.value if isinstance(s, MemoryStatus) else str(s) for s in statuses]}

    def _guard(self, scope: MemoryScope, document: dict[str, Any]) -> dict[str, Any]:
        """Assert a document really belongs to the scope that asked for it.

        The query filter already guarantees this. The check is here because a
        silent cross-tenant read is the single worst failure this system could
        have, and an assertion costs a dictionary comparison.
        """
        if not scope.matches(document):
            logger.error(
                "tenant_isolation_violation",
                expected=scope.partition_key,
                found=f"{document.get('clientId')}/{document.get('botId')}/{document.get('userId')}",
                memory_id=document.get("_id"),
            )
            raise TenantIsolationError("Memory does not belong to the requesting scope")
        return document

    @staticmethod
    def _validity_filter(as_of: datetime) -> dict[str, Any]:
        """Memories whose validity window contains ``as_of``.

        An absent bound means "unknown", not "excluded": a memory with no
        ``validFrom`` is treated as having always been true, and one with no
        ``validUntil`` as still true.
        """
        return {
            "$and": [
                {"$or": [{"validFrom": None}, {"validFrom": {"$lte": as_of}}]},
                {"$or": [{"validUntil": None}, {"validUntil": {"$gt": as_of}}]},
            ]
        }

    def _build_filter(
        self,
        scope: MemoryScope,
        *,
        statuses: list[MemoryStatus] | tuple[MemoryStatus, ...] | None = None,
        memory_types: list[str] | None = None,
        exclude_types: list[str] | None = None,
        as_of: datetime | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = scope.as_filter()

        status_filter = self._statuses(statuses)
        if status_filter is not None:
            query["status"] = status_filter

        if memory_types:
            query["memoryType"] = {"$in": [t.strip().lower() for t in memory_types]}
        elif exclude_types:
            query["memoryType"] = {"$nin": [t.strip().lower() for t in exclude_types]}

        if as_of is not None:
            query.update(self._validity_filter(as_of))
        if extra:
            query.update(extra)
        return query

    # --- writes -------------------------------------------------------------- #

    async def insert(self, memory: Memory) -> Memory:
        await self._db.memories.insert_one(memory.to_document())
        return memory

    async def insert_many(self, memories: list[Memory]) -> list[Memory]:
        if not memories:
            return []
        await self._db.memories.insert_many([m.to_document() for m in memories])
        return memories

    async def update_fields(
        self, scope: MemoryScope, memory_id: str, fields: dict[str, Any]
    ) -> Memory | None:
        """Patch a memory in place, always bumping ``updatedAt``."""
        fields = {**fields, "updatedAt": utcnow()}
        document = await self._db.memories.find_one_and_update(
            {"_id": memory_id, **scope.as_filter()},
            {"$set": fields},
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            return None
        return Memory.from_document(self._guard(scope, document))

    async def replace(self, scope: MemoryScope, memory: Memory) -> Memory:
        memory.touch()
        await self._db.memories.replace_one(
            {"_id": memory.memory_id, **scope.as_filter()}, memory.to_document()
        )
        return memory

    # --- lifecycle transitions ------------------------------------------------ #

    async def mark_superseded(
        self,
        scope: MemoryScope,
        memory_ids: list[str],
        *,
        superseded_by: str,
        valid_until: datetime | None = None,
        operation_id: str | None = None,
    ) -> list[str]:
        """Move active memories to ``superseded``, pointing at their replacement.

        Only ``active`` rows transition. A memory that was already superseded or
        merged is history and must not be rewritten — silently skipping it makes
        a replayed Dream operation idempotent instead of corrupting the chain.
        """
        if not memory_ids:
            return []

        updates: dict[str, Any] = {
            "status": MemoryStatus.SUPERSEDED.value,
            "supersededBy": superseded_by,
            "updatedAt": utcnow(),
        }
        if valid_until is not None:
            updates["validUntil"] = valid_until
        if operation_id:
            updates["metadata.supersededByOperation"] = operation_id

        result = await self._db.memories.update_many(
            {
                "_id": {"$in": memory_ids},
                **scope.as_filter(),
                "status": MemoryStatus.ACTIVE.value,
            },
            {"$set": updates},
        )
        if result.modified_count != len(memory_ids):
            logger.info(
                "supersede_partial",
                requested=len(memory_ids),
                modified=result.modified_count,
                detail="targets already in a terminal state were skipped",
            )
        return await self._transitioned_ids(scope, memory_ids, MemoryStatus.SUPERSEDED)

    async def mark_merged(
        self,
        scope: MemoryScope,
        memory_ids: list[str],
        *,
        merged_into: str,
        operation_id: str | None = None,
    ) -> list[str]:
        """Move active memories to ``merged``, pointing at the canonical row."""
        if not memory_ids:
            return []
        if merged_into in memory_ids:
            raise InvalidStateTransitionError("A memory cannot be merged into itself")

        updates: dict[str, Any] = {
            "status": MemoryStatus.MERGED.value,
            "mergedInto": merged_into,
            "updatedAt": utcnow(),
        }
        if operation_id:
            updates["metadata.mergedByOperation"] = operation_id

        await self._db.memories.update_many(
            {
                "_id": {"$in": memory_ids},
                **scope.as_filter(),
                "status": MemoryStatus.ACTIVE.value,
            },
            {"$set": updates},
        )
        return await self._transitioned_ids(scope, memory_ids, MemoryStatus.MERGED)

    async def mark_expired(
        self, scope: MemoryScope, memory_ids: list[str], *, valid_until: datetime | None = None
    ) -> list[str]:
        if not memory_ids:
            return []
        updates: dict[str, Any] = {
            "status": MemoryStatus.EXPIRED.value,
            "updatedAt": utcnow(),
        }
        if valid_until is not None:
            updates["validUntil"] = valid_until
        await self._db.memories.update_many(
            {
                "_id": {"$in": memory_ids},
                **scope.as_filter(),
                "status": MemoryStatus.ACTIVE.value,
            },
            {"$set": updates},
        )
        return await self._transitioned_ids(scope, memory_ids, MemoryStatus.EXPIRED)

    async def _transitioned_ids(
        self, scope: MemoryScope, memory_ids: list[str], status: MemoryStatus
    ) -> list[str]:
        """Ids that actually reached ``status`` — the honest result of a transition."""
        cursor = self._db.memories.find(
            {"_id": {"$in": memory_ids}, **scope.as_filter(), "status": status.value},
            {"_id": 1},
        )
        return [doc["_id"] async for doc in cursor]

    async def mark_stale(
        self, scope: MemoryScope, memory_ids: list[str], stale: bool = True
    ) -> int:
        """Flag patterns for re-evaluation. Never deletes them (see §32)."""
        if not memory_ids:
            return 0
        result = await self._db.memories.update_many(
            {"_id": {"$in": memory_ids}, **scope.as_filter()},
            {"$set": {"stale": stale, "updatedAt": utcnow()}},
        )
        return result.modified_count

    # --- reads ---------------------------------------------------------------- #

    async def get(self, scope: MemoryScope, memory_id: str) -> Memory | None:
        document = await self._db.memories.find_one({"_id": memory_id, **scope.as_filter()})
        if document is None:
            return None
        return Memory.from_document(self._guard(scope, document))

    async def get_many(self, scope: MemoryScope, memory_ids: list[str]) -> list[Memory]:
        """Fetch by id, preserving the caller's ordering.

        Ordering matters because callers pass ids in relevance order from the
        vector index and expect that order back.
        """
        if not memory_ids:
            return []
        cursor = self._db.memories.find({"_id": {"$in": memory_ids}, **scope.as_filter()})
        found = {
            doc["_id"]: Memory.from_document(self._guard(scope, doc)) async for doc in cursor
        }
        return [found[mid] for mid in memory_ids if mid in found]

    async def find(
        self,
        scope: MemoryScope,
        *,
        statuses: list[MemoryStatus] | tuple[MemoryStatus, ...] | None = None,
        memory_types: list[str] | None = None,
        exclude_types: list[str] | None = None,
        as_of: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        sort_field: str = "updatedAt",
        include_embedding: bool = True,
    ) -> list[Memory]:
        query = self._build_filter(
            scope,
            statuses=statuses,
            memory_types=memory_types,
            exclude_types=exclude_types,
            as_of=as_of,
        )
        projection = None if include_embedding else {"embedding": 0}
        cursor = (
            self._db.memories.find(query, projection)
            .sort(sort_field, DESCENDING)
            .skip(max(0, offset))
            .limit(max(1, limit))
        )
        return [Memory.from_document(self._guard(scope, doc)) async for doc in cursor]

    async def eligible_ids(
        self,
        scope: MemoryScope,
        *,
        statuses: list[MemoryStatus] | tuple[MemoryStatus, ...] | None = None,
        memory_types: list[str] | None = None,
        exclude_types: list[str] | None = None,
        as_of: datetime | None = None,
    ) -> set[str]:
        """The id set the vector index is allowed to return.

        This is the pre-filter: metadata narrowing happens in Mongo, *then*
        similarity ranks what survived. Doing it the other way round would let a
        superseded or out-of-window memory displace a valid one from the top-k.
        """
        query = self._build_filter(
            scope,
            statuses=statuses,
            memory_types=memory_types,
            exclude_types=exclude_types,
            as_of=as_of,
        )
        cursor = self._db.memories.find(query, {"_id": 1})
        return {doc["_id"] async for doc in cursor}

    async def count(
        self,
        scope: MemoryScope,
        statuses: list[MemoryStatus] | tuple[MemoryStatus, ...] | None = None,
        memory_types: list[str] | None = None,
        exclude_types: list[str] | None = None,
    ) -> int:
        query = self._build_filter(
            scope, statuses=statuses, memory_types=memory_types, exclude_types=exclude_types
        )
        return await self._db.memories.count_documents(query)

    async def count_since(self, scope: MemoryScope, since=None) -> int:
        """Active memories created after ``since`` — the auto-trigger's signal.

        ``since=None`` means the user has never been synthesised, so every
        memory counts as new.
        """
        query: dict[str, Any] = {
            **scope.as_filter(),
            "status": MemoryStatus.ACTIVE.value,
            "memoryType": {"$ne": MemoryType.PATTERN.value},
        }
        if since is not None:
            query["createdAt"] = {"$gt": since}
        return await self._db.memories.count_documents(query)

    async def find_by_content_hash(
        self,
        scope: MemoryScope,
        content_hash: str,
        statuses: tuple[MemoryStatus, ...] = (MemoryStatus.ACTIVE,),
    ) -> Memory | None:
        """Exact-duplicate lookup — deterministic, no LLM, no embedding."""
        document = await self._db.memories.find_one(
            {
                **scope.as_filter(),
                "contentHash": content_hash,
                "status": self._statuses(list(statuses)),
            }
        )
        if document is None:
            return None
        return Memory.from_document(self._guard(scope, document))

    async def find_patterns(
        self, scope: MemoryScope, *, only_active: bool = True, limit: int = 500
    ) -> list[Memory]:
        return await self.find(
            scope,
            statuses=[MemoryStatus.ACTIVE] if only_active else None,
            memory_types=[MemoryType.PATTERN.value],
            limit=limit,
        )

    async def find_patterns_referencing(
        self, scope: MemoryScope, source_memory_ids: list[str]
    ) -> list[Memory]:
        """Patterns whose evidence includes any of these memories.

        Used after a supersede or merge to flag the patterns that rested on the
        changed memory, so they can be re-evaluated on the next synthesis run.
        """
        if not source_memory_ids:
            return []
        cursor = self._db.memories.find(
            {
                **scope.as_filter(),
                "memoryType": MemoryType.PATTERN.value,
                "sourceMemoryIds": {"$in": source_memory_ids},
            }
        )
        return [Memory.from_document(self._guard(scope, doc)) async for doc in cursor]

    async def iter_all(
        self, scope: MemoryScope, batch_size: int = 500
    ) -> list[Memory]:
        """Every memory in a scope, embeddings included. For index rebuilds."""
        cursor = self._db.memories.find(scope.as_filter()).batch_size(batch_size)
        return [Memory.from_document(self._guard(scope, doc)) async for doc in cursor]

    async def distinct_scopes(self, limit: int = 10000) -> list[dict[str, str]]:
        """Every (client, bot, user) triple that has memories.

        Drives scheduled synthesis, which must walk users without being told who
        they are.
        """
        pipeline = [
            {
                "$group": {
                    "_id": {"clientId": "$clientId", "botId": "$botId", "userId": "$userId"},
                    "count": {"$sum": 1},
                }
            },
            {"$limit": limit},
        ]
        cursor = self._db.memories.aggregate(pipeline)
        return [{**doc["_id"], "count": doc["count"]} async for doc in cursor]

    # --- relations -------------------------------------------------------------- #

    async def add_relation(
        self,
        scope: MemoryScope,
        from_memory_id: str,
        to_memory_id: str,
        relation_type: RelationType,
        *,
        operation_id: str | None = None,
        bidirectional: bool = True,
    ) -> None:
        """Record an edge, and by default its inverse.

        Duplicate edges are ignored rather than raising: re-running a Dream
        operation must converge on the same graph, not fail.
        """
        if from_memory_id == to_memory_id:
            return

        edges = [(from_memory_id, to_memory_id, relation_type)]
        if bidirectional:
            edges.append((to_memory_id, from_memory_id, relation_type.inverse))

        for source, target, edge_type in edges:
            relation = MemoryRelation(
                client_id=scope.client_id,
                bot_id=scope.bot_id,
                user_id=scope.user_id,
                from_memory_id=source,
                to_memory_id=target,
                relation_type=edge_type,
                operation_id=operation_id,
            )
            document = relation.to_document()
            # Upsert on the natural key rather than insert-and-catch. The edge is
            # written once however many times a Dream operation is replayed, and
            # idempotency does not depend on a unique index existing.
            try:
                await self._db.relations.update_one(
                    {
                        "fromMemoryId": source,
                        "toMemoryId": target,
                        "relationType": edge_type.value,
                    },
                    {"$setOnInsert": document},
                    upsert=True,
                )
            except DuplicateKeyError:
                # Two writers racing the same edge: the edge exists, which is all
                # the caller wanted.
                continue

    async def add_relations(
        self,
        scope: MemoryScope,
        from_memory_id: str,
        to_memory_ids: list[str],
        relation_type: RelationType,
        *,
        operation_id: str | None = None,
    ) -> None:
        for target in to_memory_ids:
            await self.add_relation(
                scope, from_memory_id, target, relation_type, operation_id=operation_id
            )

    async def get_relations(
        self,
        scope: MemoryScope,
        memory_id: str,
        relation_types: list[RelationType] | None = None,
    ) -> list[MemoryRelation]:
        """Outgoing edges from a memory."""
        query: dict[str, Any] = {**scope.as_filter(), "fromMemoryId": memory_id}
        if relation_types:
            query["relationType"] = {"$in": [r.value for r in relation_types]}
        cursor = self._db.relations.find(query)
        return [MemoryRelation.from_document(doc) async for doc in cursor]

    async def get_incoming_relations(
        self,
        scope: MemoryScope,
        memory_id: str,
        relation_types: list[RelationType] | None = None,
    ) -> list[MemoryRelation]:
        query: dict[str, Any] = {**scope.as_filter(), "toMemoryId": memory_id}
        if relation_types:
            query["relationType"] = {"$in": [r.value for r in relation_types]}
        cursor = self._db.relations.find(query)
        return [MemoryRelation.from_document(doc) async for doc in cursor]

    # --- destructive ------------------------------------------------------------- #
    #
    # Nothing in the Dream layer reaches these. They exist for deliberate operator
    # action — a wrong extraction, a "forget that" request, an erasure request —
    # and for tests. Everything here is irreversible.

    async def delete_memories(self, scope: MemoryScope, memory_ids: list[str]) -> int:
        """Permanently remove specific memories and every edge touching them.

        Edges are removed in both directions: leaving a dangling `supersedes`
        pointing at a deleted row would make the graph unreadable.
        """
        if not memory_ids:
            return 0

        result = await self._db.memories.delete_many(
            {"_id": {"$in": memory_ids}, **scope.as_filter()}
        )
        await self._db.relations.delete_many(
            {
                **scope.as_filter(),
                "$or": [
                    {"fromMemoryId": {"$in": memory_ids}},
                    {"toMemoryId": {"$in": memory_ids}},
                ],
            }
        )

        # A memory pointing at something that no longer exists is worse than one
        # pointing at nothing, so clear the inbound pointers too.
        await self._db.memories.update_many(
            {**scope.as_filter(), "supersededBy": {"$in": memory_ids}},
            {"$set": {"supersededBy": None}},
        )
        await self._db.memories.update_many(
            {**scope.as_filter(), "mergedInto": {"$in": memory_ids}},
            {"$set": {"mergedInto": None}},
        )
        await self._db.memories.update_many(
            {**scope.as_filter(), "sourceMemoryIds": {"$in": memory_ids}},
            {"$pull": {"sourceMemoryIds": {"$in": memory_ids}}},
        )

        logger.warning(
            "memories_hard_deleted",
            count=result.deleted_count,
            memory_ids=memory_ids[:10],
        )
        return result.deleted_count

    async def delete_scope(self, scope: MemoryScope) -> dict[str, int]:
        """Hard-delete everything for one user: memories, edges, audit and jobs.

        This is the right-to-erasure path, so it removes the audit trail as well.
        Retaining "we decided to store X about this user" after deleting X would
        defeat the point of the request.
        """
        memories = await self._db.memories.delete_many(scope.as_filter())
        relations = await self._db.relations.delete_many(scope.as_filter())
        operations = await self._db.operations.delete_many(scope.as_filter())
        jobs = await self._db.jobs.delete_many(scope.as_filter())

        logger.warning(
            "scope_purged",
            memories=memories.deleted_count,
            relations=relations.deleted_count,
            operations=operations.deleted_count,
            jobs=jobs.deleted_count,
        )
        return {
            "memories": memories.deleted_count,
            "relations": relations.deleted_count,
            "operations": operations.deleted_count,
            "jobs": jobs.deleted_count,
        }
