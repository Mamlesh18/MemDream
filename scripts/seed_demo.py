"""Seed a realistically *aged* account, so consolidation has something to do.

    python -m scripts.seed_demo --user demo_priya
    python -m scripts.seed_demo --user demo_priya --wipe

Every part of consolidation keys off time — how old a memory is, when it was
last recalled, whether its validity window has closed. A fresh account has none
of that, so the features look like they do nothing. This writes an account that
has been running for a year.

Nothing here goes through extraction or the Dream engine: the point is to
control `createdAt`, `lastRecalledAt` and `validUntil` exactly, which a
conversation cannot do. It is a fixture, not a demo of the write path — use
`examples/embedded.py` for that.

What it creates, and why each group exists:

    the important ones    high importance, never recalled. These must survive
                          everything. If a run retires the peanut allergy, the
                          guard rails are wrong.
    the lapsed ones       validUntil already past. Provably false, and the one
                          thing Phase 1 retires without a model.
    the forgotten ones    low importance, old, never recalled. What decay is
                          for — and what it must not touch until the epoch has
                          been running long enough.
    the crowd             fifty near-identical order lines. What compaction is
                          for: individually worthless, collectively expensive.
    the evidence          low importance, old, unused — but cited by a pattern.
                          Must survive both decay and compaction.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.container import DreamContainer  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.models.enums import MemoryType, TemporalNature  # noqa: E402
from app.models.memory import Memory, utcnow  # noqa: E402
from app.models.scope import MemoryScope  # noqa: E402

NOW = utcnow()

IMPORTANT = [
    ("User is allergic to peanuts.", 0.95, "constraint"),
    ("User's delivery address is 22 Anna Nagar, Chennai 600040.", 0.85, "fact"),
    ("User's name is Priya Raman.", 0.90, "profile"),
    ("User does not eat beef.", 0.70, "preference"),
]

LAPSED = [
    ("User is on leave until the end of last month.", 0.30, 400, 20),
    ("User is hosting guests this week.", 0.35, 380, 5),
]

FORGOTTEN = [
    "User mentioned enjoying jazz on the radio.",
    "User once asked whether the kitchen uses ghee.",
    "User commented that the delivery bag was torn.",
    "User said the weather was unusually cold that evening.",
]

DISHES = ["masala dosa", "idli sambar", "veg thali", "paneer butter masala",
          "curd rice", "rava dosa", "lemon rice", "vada"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


async def build(container, scope, *, crowd_size: int) -> dict[str, int]:
    random.seed(7)  # reproducible, so two runs describe the same account
    counts = {"important": 0, "lapsed": 0, "forgotten": 0, "crowd": 0, "evidence": 0}

    async def write(content, *, importance, memory_type="other", age_days,
                    recalled_days_ago=None, valid_until=None, temporal=None,
                    source_ids=None) -> Memory:
        created = NOW - timedelta(days=age_days)
        embedding = await container.embeddings.embed(content)
        memory = Memory(
            **scope.as_model_fields(),
            content=content,
            memory_type=memory_type,
            importance=importance,
            confidence=0.9,
            explicit=source_ids is None,
            temporal_nature=temporal or TemporalNature.CURRENT,
            created_at=created,
            updated_at=created,
            valid_until=valid_until,
            last_recalled_at=(
                NOW - timedelta(days=recalled_days_ago)
                if recalled_days_ago is not None else None
            ),
            recall_count=0 if recalled_days_ago is None else random.randint(1, 9),
            source_memory_ids=source_ids or [],
            embedding=embedding,
            embedding_model=container.embeddings.model_name,
            embedding_dim=len(embedding),
            metadata={"source": "seed"},
        )
        await container.lifecycle.activate(scope, memory)
        return memory

    # Must survive everything.
    for content, importance, kind in IMPORTANT:
        await write(content, importance=importance, memory_type=kind, age_days=300)
        counts["important"] += 1

    # Provably false — Phase 1 retires these with no model.
    for content, importance, age, lapsed_days in LAPSED:
        await write(
            content, importance=importance, age_days=age,
            valid_until=NOW - timedelta(days=lapsed_days),
        )
        counts["lapsed"] += 1

    # What decay is for.
    for offset, content in enumerate(FORGOTTEN):
        await write(content, importance=0.25, age_days=200 + offset * 10)
        counts["forgotten"] += 1

    # What compaction is for: individually worthless, collectively expensive.
    for _ in range(crowd_size):
        dish, day = random.choice(DISHES), random.choice(DAYS)
        await write(
            f"User ordered {dish} on a {day}.",
            importance=0.25, memory_type="event",
            age_days=random.randint(60, 340),
        )
        counts["crowd"] += 1

    # Old, unused and low value — but a pattern rests on it, so it must survive.
    evidence = [
        await write(f"User ran before work on a {day}.", importance=0.25, age_days=250)
        for day in DAYS[:4]
    ]
    counts["evidence"] = len(evidence)
    await write(
        "User exercises early on most weekday mornings.",
        importance=0.6, memory_type=MemoryType.PATTERN.value, age_days=240,
        source_ids=[m.memory_id for m in evidence],
    )

    return counts


async def main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging("ERROR", json_output=False)
    container = await DreamContainer.create(settings, ensure_schema=True)
    scope = MemoryScope(user_id=args.user, client_id=args.client)

    try:
        if args.wipe:
            counts = await container.memory_service.purge_user(scope)
            print(f"wiped: {counts}")

        counts = await build(container, scope, crowd_size=args.crowd)
        total = sum(counts.values()) + 1  # + the pattern
        print(f"seeded {total} memories for {args.user}")
        for name, count in counts.items():
            print(f"  {name:<12} {count}")
        print(f"\nscopeKey {scope.scope_key}")
        print("\nNow try:")
        print("  python -m scripts.consolidate --user " + args.user + " --dry-run")
    finally:
        await container.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--user", default="demo_priya")
    parser.add_argument("--client", default="seed_demo")
    parser.add_argument("--crowd", type=int, default=50,
                        help="near-identical order lines, the compaction fodder")
    parser.add_argument("--wipe", action="store_true",
                        help="erase this user first, so re-seeding is repeatable")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
