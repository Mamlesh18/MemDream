"""Rebuild the vector index from MongoDB.

    python -m scripts.rebuild_index                    # every user
    python -m scripts.rebuild_index --client-id acme   # one client
    python -m scripts.rebuild_index --re-embed         # recompute embeddings too

Mongo is the source of truth and stores every embedding, so the index is a
derived artefact that can be deleted and rebuilt at any time. Reach for this
after a corrupt index, a restore from backup, or a change of embedding model
(with `--re-embed`, which costs one embedding call per memory).

Merged and expired memories are deliberately left out: their content lives on in
the memory that absorbed them, and re-indexing them would return the same fact
twice.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.container import DreamContainer
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.models.enums import MemoryStatus
from app.models.scope import MemoryScope

logger = get_logger(__name__)

#: Statuses that stay searchable. Superseded memories are included — historical
#: retrieval is a feature, not a leak.
INDEXED_STATUSES = [MemoryStatus.ACTIVE, MemoryStatus.SUPERSEDED]


async def rebuild_scope(
    container: DreamContainer, scope: MemoryScope, *, re_embed: bool, batch_size: int
) -> tuple[int, int]:
    """Rebuild one user's partition. Returns (indexed, skipped)."""
    await container.vectors.clear(scope)

    memories = await container.memories.find(
        scope, statuses=INDEXED_STATUSES, limit=1_000_000
    )
    if not memories:
        return 0, 0

    if re_embed:
        # Batched, because one call per memory is both slow and needlessly
        # expensive against a rate-limited deployment.
        for start in range(0, len(memories), batch_size):
            chunk = memories[start : start + batch_size]
            vectors = await container.embeddings.embed_batch([m.content for m in chunk])
            # strict: a provider returning fewer vectors than inputs would
            # otherwise silently leave the tail of the chunk un-embedded.
            for memory, vector in zip(chunk, vectors, strict=True):
                memory.embedding = vector
                memory.embedding_model = container.embeddings.model_name
                memory.embedding_dim = len(vector)
                await container.memories.update_fields(
                    scope,
                    memory.memory_id,
                    {
                        "embedding": vector,
                        "embeddingModel": memory.embedding_model,
                        "embeddingDim": memory.embedding_dim,
                    },
                )

    indexed = [memory for memory in memories if memory.embedding]
    skipped = len(memories) - len(indexed)

    for start in range(0, len(indexed), batch_size):
        await container.retriever.index_memories(scope, indexed[start : start + batch_size])

    if skipped:
        logger.warning(
            "rebuild_skipped_memories",
            user_id=scope.user_id,
            skipped=skipped,
            hint="memories without an embedding; re-run with --re-embed",
        )
    return len(indexed), skipped


async def main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=False)

    container = await DreamContainer.create(settings)
    try:
        scopes = await container.memories.distinct_scopes()

        if args.client_id:
            scopes = [s for s in scopes if s.get("clientId") == args.client_id]
        if args.bot_id:
            scopes = [s for s in scopes if s.get("agentId") == args.bot_id]
        if args.user_id:
            scopes = [s for s in scopes if s["userId"] == args.user_id]

        if not scopes:
            print("No matching users found.")
            return 0

        total_memories = sum(entry["count"] for entry in scopes)
        print(f"Rebuilding {len(scopes)} partition(s), {total_memories} memories.")
        if args.re_embed:
            print(f"Re-embedding with {container.embeddings.model_name} — this will cost tokens.")
            if not args.yes and input("Continue? [y/N] ").strip().lower() != "y":
                print("Aborted.")
                return 1

        indexed_total = skipped_total = 0
        for position, entry in enumerate(scopes, 1):
            # Every dimension: a scope missing one computes a scopeKey that
            # matches none of this user's memories, and the rebuild would
            # silently index nothing.
            scope = MemoryScope(
                user_id=entry["userId"],
                agent_id=entry.get("agentId"),
                client_id=entry.get("clientId"),
                namespace=entry.get("namespace") or {},
            )
            indexed, skipped = await rebuild_scope(
                container, scope, re_embed=args.re_embed, batch_size=args.batch_size
            )
            indexed_total += indexed
            skipped_total += skipped
            print(f"  [{position}/{len(scopes)}] {scope.user_id}: {indexed} indexed")

        print(f"\nDone. {indexed_total} vectors indexed, {skipped_total} skipped.")
        return 0
    finally:
        await container.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild the vector index from MongoDB")
    parser.add_argument("--client-id", help="restrict to one client")
    parser.add_argument("--bot-id", help="restrict to one bot")
    parser.add_argument("--user-id", help="restrict to one user")
    parser.add_argument(
        "--re-embed",
        action="store_true",
        help="recompute embeddings via the provider (needed after a model change)",
    )
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
