"""Dream Memory with no infrastructure at all.

    python examples/quickstart.py

Runs the whole framework in-process -- fake providers, in-memory vector store,
mongomock -- and walks through every behaviour the system exists to provide.
No MongoDB, no Redis, no Azure credentials, no network, no cost.

To run the same script against a real deployment, set:

    DREAM_LLM_PROVIDER=azure_openai
    DREAM_EMBEDDING_PROVIDER=azure_openai
    AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_API_KEY=...

and delete the `database=` / `vectors=` / `queue=` overrides below.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Runnable straight from a checkout, without installing the package first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mongomock_motor import AsyncMongoMockClient

from app.container import DreamContainer
from app.core.config import Settings
from app.core.identity import build_scope
from app.core.logging import configure_logging
from app.models.enums import MemoryStatus
from app.models.requests import SearchMemoryRequest
from app.providers.queue.inline_queue import InlineJobQueue
from app.providers.vector_store.memory_store import InMemoryVectorStore
from app.repositories.mongo import MongoDatabase

BOT = "bot_demo"
PHONE = "+919999999999"


def heading(title: str) -> None:
    print(f"\n{'-' * 70}\n{title}\n{'-' * 70}")


async def build(client_id: str = "client_demo"):
    settings = Settings(
        env="demo",
        identity_salt="demo-salt",
        llm_provider="fake",
        embedding_provider="fake",
        vector_provider="memory",
        queue_provider="inline",
        log_level="ERROR",
        retrieval={"similarity_threshold": 0.15},
        memory={"min_confidence": 0.4, "min_importance": 0.05},
        dream={
            "supersede": {"similarity_threshold": 0.05},
            "merge": {"similarity_threshold": 0.15},
            "synthesis": {
                "min_memories": 4,
                "min_cluster_size": 3,
                # High enough that unrelated facts do not group together. The
                # offline bag-of-words embedder scores unrelated short texts
                # around 0.1-0.2, so a lower cut-off clusters everything.
                "cluster_similarity_threshold": 0.22,
                "min_pattern_confidence": 0.4,
                "min_pattern_importance": 0.2,
            },
        },
    )
    container = await DreamContainer.create(
        settings,
        database=MongoDatabase(AsyncMongoMockClient(tz_aware=True), "memdream_demo"),
        vectors=InMemoryVectorStore(),
        queue=InlineJobQueue(),
        ensure_schema=False,
    )
    scope = build_scope(
        client_id=client_id, bot_id=BOT, phone_number=PHONE, settings=settings
    )
    return container, scope, settings


async def remember(container, scope, message: str) -> None:
    outcome = await container.memory_service.add(scope, message)
    if not outcome.results:
        print(f'  "{message}"\n      -> nothing durable ({outcome.reason})')
        return

    print(f'  "{message}"')
    for result in outcome.results:
        line = f"      -> {result.action.value}"
        if result.memory:
            line += f'  "{result.memory.content}"'
        print(line)
        if result.superseded_memory_ids:
            print(f"         supersedes {', '.join(result.superseded_memory_ids)}")
        if result.merged_memory_ids:
            print(f"         merges {', '.join(result.merged_memory_ids)}")
        print(f"         reason: {result.reason}")


async def search(container, scope, query: str, **kwargs) -> None:
    results = await container.memory_service.search(
        scope,
        SearchMemoryRequest(
            query=query,
            client_id=scope.client_id,
            bot_id=scope.bot_id,
            user_id=scope.user_id,
            **kwargs,
        ),
    )
    print(f'  "{query}"')
    if not results:
        print("      (nothing)")
    for scored in results:
        marker = "" if scored.memory.status == MemoryStatus.ACTIVE else f"  [{scored.memory.status.value}]"
        print(f"      {scored.score:.3f}  {scored.memory.content}{marker}")


async def main() -> None:
    configure_logging("ERROR", json_output=False)
    container, scope, settings = await build()

    try:
        # -- 1. ADD ----------------------------------------------------------
        heading("1. Remembering new facts")
        for message in (
            "I live in Chennai.",
            "I like black coffee.",
            "Hi there!",  # nothing durable -- a greeting is not a memory
        ):
            await remember(container, scope, message)

        # -- 2. NOOP ---------------------------------------------------------
        heading("2. Saying the same thing again")
        print("  An exact repeat is caught by a hash lookup -- no model call at all.")
        await remember(container, scope, "I like black coffee.")

        # -- 3. SUPERSEDE ----------------------------------------------------
        heading("3. Contradicting an earlier fact")
        await remember(container, scope, "I moved to Bangalore.")

        # -- 4. Non-destructive history --------------------------------------
        heading("4. Nothing was deleted")
        for memory in await container.memories.find(scope, limit=20):
            pointer = f"  -> {memory.superseded_by}" if memory.superseded_by else ""
            print(f"  [{memory.status.value:<10}] {memory.content}{pointer}")

        # -- 5. Retrieval ----------------------------------------------------
        heading("5. Recall: current answer vs full history")
        print("\n  latestOnly=true -- what is true now:")
        await search(container, scope, "Where does the user live?", latest_only=True)
        print("\n  default -- history included, ranked below current facts:")
        await search(container, scope, "Where does the user live?")

        # -- 6. Time travel --------------------------------------------------
        heading("6. Walking the supersede chain")
        current = (await container.memories.find(
            scope, statuses=[MemoryStatus.ACTIVE], memory_types=["profile"], limit=1
        ))
        if current:
            for entry in await container.memory_service.history(scope, current[0].memory_id):
                window = f"{entry.valid_from:%Y-%m-%d} -> {entry.valid_until:%Y-%m-%d}" if entry.valid_until else f"{entry.valid_from:%Y-%m-%d} -> now"
                print(f"  {entry.content:<38} {window}")

        # -- 7. Synthesis ----------------------------------------------------
        heading("7. Dreaming: patterns across many memories")
        for message in (
            "I prefer morning meetings.",
            "I schedule meetings before 10 AM.",
            "I start work early every day.",
            "I attend early morning calls.",
            "I block my mornings for focused work.",
        ):
            await container.memory_service.add(scope, message)

        job = await container.dream_service.request_synthesis(scope, wait=True)
        print(f"  analysed {job.memories_analyzed} memories in "
              f"{job.clusters_evaluated} cluster(s) -> {job.patterns_created} pattern(s)")

        for pattern in await container.memories.find_patterns(scope):
            print(f"\n  PATTERN  {pattern.content}")
            print(f"           confidence {pattern.confidence:.2f}  "
                  f"explicit={pattern.explicit}")
            print(f"           derived from {len(pattern.source_memory_ids)} memories:")
            for source in await container.memories.get_many(scope, pattern.source_memory_ids):
                print(f"             * {source.content}")

        # -- 8. Idempotency --------------------------------------------------
        heading("8. Dreaming again over the same evidence")
        before = len(await container.memories.find_patterns(scope))
        for _ in range(3):
            await container.dream_service.request_synthesis(scope, wait=True, force=True)
        after = len(await container.memories.find_patterns(scope))
        print(f"  patterns before: {before}   after 3 more runs: {after}")
        print("  -> idempotent" if before == after else "  -> DUPLICATED (bug)")

        # -- 9. Tenant isolation ---------------------------------------------
        heading("9. Tenant isolation")
        other_scope = build_scope(
            client_id="client_other", bot_id=BOT, phone_number=PHONE, settings=settings
        )
        await container.memory_service.add(other_scope, "I live in Chennai.")

        print("  same phone number, two clients:")
        print(f"    client_demo  -> {scope.user_id}")
        print(f"    client_other -> {other_scope.user_id}")
        print(f"    identifiers differ: {scope.user_id != other_scope.user_id}")

        mine = {m.memory_id for m in await container.memories.find(scope, limit=100)}
        theirs = {m.memory_id for m in await container.memories.find(other_scope, limit=100)}
        print(f"    memory sets disjoint: {mine.isdisjoint(theirs)}")

        # -- 10. Audit -------------------------------------------------------
        heading("10. Every decision is explainable")
        for operation in await container.audit.list_for_scope(scope, limit=6):
            print(f"  {operation.action:<10} {operation.reason[:60]}")

    finally:
        await container.close()


if __name__ == "__main__":
    asyncio.run(main())
