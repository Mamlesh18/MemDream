"""Run consolidation by hand, and see exactly what it would do.

    python -m scripts.consolidate --user demo_priya --dry-run
    python -m scripts.consolidate --user demo_priya --decay-after 30 --dry-run
    python -m scripts.consolidate --all --dry-run

`--dry-run` is the mode that matters. Decay and compaction both ship disabled
because their thresholds have to be *measured* rather than guessed, and this is
the instrument you measure with: point it at real data, vary the window, and
watch which memories fall out before you let anything act on them.

The flags override configuration for this invocation only. Nothing is written
unless you drop `--dry-run`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.container import DreamContainer  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.models.enums import MemoryStatus  # noqa: E402
from app.models.scope import MemoryScope  # noqa: E402


async def report(container, scope, args) -> tuple[int, int]:
    active, _ = await container.memory_service.list_memories(
        scope, statuses=[MemoryStatus.ACTIVE], limit=500
    )
    print(f"\n{scope.user_id}  —  {len(active)} active")

    result = await container.consolidation.run(scope, dry_run=args.dry_run)
    lookup = {m.memory_id: m for m in active}

    for memory_id in result.lapsed:
        memory = lookup.get(memory_id)
        print(f"  LAPSED   {memory.content[:62] if memory else memory_id}")
    for memory_id in result.decayed:
        memory = lookup.get(memory_id)
        mark = "DECAY  " if args.decay else "would  "
        recalled = "never" if memory and not memory.last_recalled_at else "old"
        print(f"  {mark}  [{recalled:>5}] {memory.content[:56] if memory else memory_id}")
    for memory_id in result.protected:
        memory = lookup.get(memory_id)
        print(f"  KEPT     pattern evidence — {memory.content[:44] if memory else memory_id}")

    folded = None
    if args.compact:
        folded = await container.compaction.run(scope, dry_run=args.dry_run)
        if folded.clusters:
            print(f"  COMPACT  {folded.clusters} cluster(s), "
                  f"{folded.absorbed} memories -> {len(folded.compacted)} summaries"
                  f"{f', {folded.rejected} refused' if folded.rejected else ''}")
        for memory_id in folded.compacted:
            memory = await container.memories.get(scope, memory_id)
            if memory:
                print(f"           {memory.content[:66]}")

    return result.expired_count + len(result.decayed), (folded.absorbed if folded else 0)


async def main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(args.log, json_output=False)

    # Flags override configuration for this run only.
    consolidation = settings.dream.consolidation
    if args.decay:
        consolidation.decay_enabled = True
    if args.decay_after is not None:
        consolidation.decay_after_days = args.decay_after
    if args.epoch:
        consolidation.recall_epoch = args.epoch
    if args.importance_floor is not None:
        consolidation.decay_importance_floor = args.importance_floor
    if args.compact:
        settings.dream.compaction.enabled = True

    container = await DreamContainer.create(settings, ensure_schema=False)
    try:
        print(f"decay {'ON' if consolidation.decay_enabled else 'off'} · "
              f"window {consolidation.decay_after_days}d · "
              f"floor {consolidation.decay_importance_floor} · "
              f"epoch {consolidation.recall_epoch or 'unset'} · "
              f"compaction {'ON' if args.compact else 'off'}"
              f"{'   [DRY RUN]' if args.dry_run else ''}")

        if args.all:
            scopes = [
                MemoryScope(
                    user_id=e["userId"], agent_id=e.get("agentId"),
                    client_id=e.get("clientId"), namespace=e.get("namespace") or {},
                )
                for e in await container.memories.distinct_scopes()
            ]
        else:
            scopes = [MemoryScope(user_id=args.user, client_id=args.client)]

        retired = absorbed = 0
        for scope in scopes:
            a, b = await report(container, scope, args)
            retired += a
            absorbed += b

        verb = "would retire" if args.dry_run else "retired"
        print(f"\n{verb} {retired}, {'would fold' if args.dry_run else 'folded'} {absorbed}")
    finally:
        await container.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--user", default="demo_priya")
    parser.add_argument("--client", default="seed_demo")
    parser.add_argument("--all", action="store_true", help="every partition")
    parser.add_argument("--dry-run", action="store_true", help="change nothing")
    parser.add_argument("--decay", action="store_true", help="enable recall decay")
    parser.add_argument("--decay-after", type=int, help="window in days")
    parser.add_argument("--epoch", help="when recall tracking began, ISO date")
    parser.add_argument("--importance-floor", type=float)
    parser.add_argument("--compact", action="store_true", help="enable compaction")
    parser.add_argument("--log", default="ERROR")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
