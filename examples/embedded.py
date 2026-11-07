"""MemDream with no server at all.

    python examples/embedded.py

Runs entirely offline: the deterministic fake LLM and embedding providers, the
in-process vector store, and `database="memory"`. No API keys, no containers,
no cost — so you can watch the decision logic before wiring up a real provider.

To point it at real infrastructure, replace `**OFFLINE` with real choices —
`llm="openai:gpt-4o-mini"`, `embeddings="openai:text-embedding-3-small"`,
`database="mongodb://localhost:27017/memdream"` — and set `OPENAI_API_KEY`.
Nothing else in this file changes — which is the point: the difference between
this demo and production is configuration, not code.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Runnable straight from a checkout, without installing the package first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memdream import MemDream  # noqa: E402

#: Every one of these is an ordinary setting, normally read from the environment.
OFFLINE = {
    # MemDream ships no prompts. These are the starter templates in this
    # repository; copy them into your own project with
    # `python -m memdream prompts ./prompts` and edit them.
    "prompts": str(Path(__file__).resolve().parent.parent / "prompts"),
    "llm": "fake",
    "embeddings": "fake",
    "database": "memory",
}


def main() -> None:
    with MemDream(user_id="alice", **OFFLINE) as mem:
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
