"""MemDream with no server at all.

    python examples/embedded.py

Runs entirely offline: the deterministic fake LLM and embedding providers, the
in-process vector store, and an in-memory MongoDB. No API keys, no containers,
no cost — so you can watch the decision logic before wiring up a real provider.

To point it at real infrastructure, delete `**OFFLINE` and the `database=`
argument and set the usual environment variables (`AZURE_OPENAI_API_KEY`,
`DREAM_MONGODB_URI`, ...). Nothing else in this file changes — which is the
point: the difference between this demo and production is configuration, not
code.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Runnable straight from a checkout, without installing the package first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.logging import configure_logging  # noqa: E402
from memdream import MemDream  # noqa: E402

#: Every one of these is an ordinary setting, normally read from the environment.
OFFLINE = {
    "llm_provider": "fake",
    "embedding_provider": "fake",
    "vector_provider": "memory",
    "queue_provider": "inline",
    "identity_salt": "example-salt-do-not-use-in-production",
    "log_json": False,
    "log_level": "WARNING",
}


def offline_database():
    """An in-memory MongoDB, so the example needs no database running."""
    from mongomock_motor import AsyncMongoMockClient

    from app.repositories.mongo import MongoDatabase

    return MongoDatabase(AsyncMongoMockClient(tz_aware=True), "memdream_example")


def main() -> None:
    # Otherwise structlog prints every internal event and buries the output.
    configure_logging("WARNING", json_output=False)

    with MemDream(user_id="alice", database=offline_database(), **OFFLINE) as mem:
        print("--- remembering ---")
        for line in (
            "I live in Chennai.",
            "I am allergic to peanuts.",
            "I moved to Bangalore last month.",
        ):
            result = mem.add(line)
            print(f"  {line!r:42} -> {result.action}")
            if result.superseded_memory_ids:
                print(f"     ...superseding {result.superseded_memory_ids}")

        # Chennai is gone from the active set, but not deleted. That distinction
        # is the whole design: contradictions are resolved, history is kept.
        print("\n--- what is true now ---")
        for memory in mem.list():
            print(f"  [{memory.status}] {memory.content}")

        print("\n--- recall, ready to paste into a prompt ---")
        found = mem.search("Bangalore", latest_only=True)
        print(found.as_context())

        print("\n--- and the history is still there ---")
        for entry in mem.history(found[0].memory_id):
            print(f"  [{entry.status:10}] {entry.content}")

        print("\n--- what it cost ---")
        stats = mem.stats()
        totals = stats["tokens"]["totals"]
        print(f"  memories: {stats['counts']}")
        print(f"  decisions: {stats['actions']}")
        print(
            f"  {totals['totalTokens']} tokens, "
            f"~{totals['estimatedCost']} {stats['tokens']['currency']}"
        )
        # The fake providers report plausible token counts rather than zero, so
        # the cost-accounting path is exercised offline instead of only in
        # production, where discovering it is broken is expensive.
        print("  (the offline providers report usage too, so this path is real)")


if __name__ == "__main__":
    main()
