"""Backfill ``scopeKey`` on documents written before it existed.

    python -m scripts.migrate_scope_key --dry-run     # look first
    python -m scripts.migrate_scope_key               # then do it

## Why this is needed

Scoped queries used to filter on a three-field predicate — `clientId`, `botId`,
`userId`. They now filter on `scopeKey`, a single digest of the whole scope,
because with optional dimensions a predicate has to reason about
null-versus-missing for each one and getting that wrong matches the wrong
partition. See `app/models/scope.py`.

Documents written before that change have no `scopeKey`. **They are not lost,
but they are invisible**: every query filters on a field they do not have, so a
user with years of history looks brand new. Nothing errors — which is the
dangerous part, and the reason this script reports what it finds rather than
only what it fixed.

## Why it cannot run inside MongoDB

`scopeKey` is a SHA-256 digest, and MongoDB's aggregation pipeline has no hash
operator. So each document is read, its key computed here, and the update
written back in batches.

## `botId` becomes `agentId`

`bot_id` was renamed to `agent_id`, and the digest is computed over the *new*
spelling. A document carrying only `botId` is treated as having that agent — and
`agentId` is written alongside `scopeKey`, so the row stops needing the
compatibility path on every subsequent read.

Safe to re-run: documents that already have a `scopeKey` are skipped, and the
key is deterministic, so a second pass over the same data is a no-op.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))



from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.models.scope import MemoryScope  # noqa: E402
from app.repositories.mongo import (  # noqa: E402
    JOBS,
    MEMORIES,
    OPERATIONS,
    RELATIONS,
    MongoDatabase,
)

logger = get_logger(__name__)

#: Every collection that carries a scope. All four are keyed the same way, so
#: one pass handles them all — and it must, because a memory whose audit rows
#: were left behind has a history that silently stops.
SCOPED_COLLECTIONS = (MEMORIES, RELATIONS, OPERATIONS, JOBS)


def scope_key_for(document: dict) -> str | None:
    """The digest this document's partition should have.

    Returns None when there is no `userId` to key on. That should not happen —
    it is the one required dimension — but a document without one cannot be
    placed in any partition, and guessing would put it in the wrong one.
    """
    user_id = document.get("userId")
    if not user_id:
        return None

    return MemoryScope(
        user_id=user_id,
        # The rename: a pre-rename document carries only `botId`.
        agent_id=document.get("agentId") or document.get("botId"),
        client_id=document.get("clientId"),
        namespace=document.get("namespace") or {},
    ).scope_key


#: Documents that predate the field, in all the shapes "absent" can take.
_NEEDS_KEY = {
    "$or": [{"scopeKey": {"$exists": False}}, {"scopeKey": None}, {"scopeKey": ""}]
}


async def migrate_collection(
    database: MongoDatabase, name: str, *, dry_run: bool, batch_size: int
) -> dict[str, int]:
    """Backfill one collection, grouping documents by the key they resolve to.

    Rows are grouped rather than updated one by one because everything that
    determines the digest — user, agent, client, namespace — is identical across
    a group by construction, so one `update_many` per partition does the work of
    hundreds of individual writes. A deployment has far fewer partitions than
    memories, which is what makes that the cheap direction.
    """
    collection = database.db[name]
    total = await collection.count_documents({})
    pending = await collection.count_documents(_NEEDS_KEY)

    stats = {"total": total, "pending": pending, "updated": 0, "skipped": 0}
    if pending == 0:
        return stats

    # scope_key -> (fields to set, ids to set them on)
    groups: dict[str, tuple[dict, list]] = {}
    buffered = 0

    async def flush() -> None:
        nonlocal buffered
        for updates, ids in groups.values():
            if dry_run:
                stats["updated"] += len(ids)
            else:
                result = await collection.update_many(
                    {"_id": {"$in": ids}}, {"$set": updates}
                )
                stats["updated"] += result.modified_count
        groups.clear()
        buffered = 0

    cursor = collection.find(
        _NEEDS_KEY,
        # Only what the digest is computed from. Pulling whole memories would
        # drag every embedding across the wire for nothing.
        {"userId": 1, "agentId": 1, "botId": 1, "clientId": 1, "namespace": 1},
    )

    async for document in cursor:
        key = scope_key_for(document)
        if key is None:
            logger.warning(
                "skipped_document_without_user", collection=name, _id=document.get("_id")
            )
            stats["skipped"] += 1
            continue

        if key not in groups:
            updates: dict = {"scopeKey": key}
            # Write the new spelling too, so the row stops relying on the
            # `botId` compatibility path on every future read.
            if not document.get("agentId") and document.get("botId"):
                updates["agentId"] = document["botId"]
            groups[key] = (updates, [])

        groups[key][1].append(document["_id"])
        buffered += 1
        if buffered >= batch_size:
            await flush()

    await flush()
    return stats


async def main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=False)

    database = MongoDatabase.connect(settings)
    print(f"Database: {settings.mongodb_database} ({settings.mongodb_uri.split('@')[-1]})")
    if args.dry_run:
        print("DRY RUN - nothing will be written.\n")

    try:
        await database.ping()

        grand_total = 0
        for name in SCOPED_COLLECTIONS:
            stats = await migrate_collection(
                database, name, dry_run=args.dry_run, batch_size=args.batch_size
            )
            grand_total += stats["updated"]

            verb = "would backfill" if args.dry_run else "backfilled"
            if stats["pending"] == 0:
                print(f"  {name:20} {stats['total']:>7} documents, all current")
            else:
                print(
                    f"  {name:20} {stats['total']:>7} documents, "
                    f"{verb} {stats['updated']}"
                    + (f", skipped {stats['skipped']}" if stats["skipped"] else "")
                )

        if grand_total == 0:
            print("\nNothing to do.")
        elif args.dry_run:
            print(f"\n{grand_total} documents need a scopeKey. Re-run without --dry-run.")
        else:
            print(f"\nDone. {grand_total} documents backfilled.")
            # The vector index keys on the same scope, so it is stale too.
            print(
                "\nNext: rebuild the vector index so retrieval can reach these "
                "memories —\n    python -m scripts.rebuild_index"
            )
    finally:
        await database.close()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill scopeKey on documents written before it existed."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing anything",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="documents per bulk write (default: 500)",
    )
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
