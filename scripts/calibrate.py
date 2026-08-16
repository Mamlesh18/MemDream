"""Measure the similarity distribution of your embedding model.

    python -m scripts.calibrate

Thresholds are the most consequential configuration in this framework and the
easiest to get wrong, because cosine similarity reads like a percentage and is
not one. Different embedding models occupy completely different ranges: some put
unrelated text near 0.0, others near 0.4. A threshold copied from one model's
documentation silently disables Dream operations on another.

So do not guess. This script embeds labelled pairs and reports where each
relationship actually lands for *your* deployment, then suggests thresholds from
the measurement.

Costs a few dozen embedding calls. Re-run whenever you change embedding model.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.providers.embeddings.base import cosine_similarity
from app.providers.embeddings.factory import build_embedding_provider

# (label, text A, text B). Labels group into the four bands that matter.
PAIRS: list[tuple[str, str, str]] = [
    # --- unrelated: must fall below every threshold -------------------------
    ("unrelated", "User lives in Bangalore.", "User runs 40 km per week."),
    ("unrelated", "User likes black coffee.", "User's production database is PostgreSQL."),
    ("unrelated", "User has a dog named Rex.", "User uses Kubernetes in production."),
    ("unrelated", "User is allergic to peanuts.", "User's default browser is Firefox."),
    ("unrelated", "User plays the guitar.", "User's team has 12 engineers."),
    ("unrelated", "User works at Acme.", "User prefers window seats."),
    ("unrelated", "User is training for a marathon.", "User studied at IIT Madras."),
    ("unrelated", "User's birthday is 14 March.", "User deploys to Kubernetes."),

    # --- same topic, different value: SUPERSEDE candidates -------------------
    ("contradiction", "User lives in Bangalore.", "User lives in Chennai."),
    ("contradiction", "User's production database is PostgreSQL.", "User's production database is MongoDB."),
    ("contradiction", "User works at Globex.", "User works at Acme."),
    ("contradiction", "User prefers light mode.", "User prefers dark mode."),
    ("contradiction", "User's cloud provider is Azure.", "User's cloud provider is AWS."),
    ("contradiction", "User goes to the gym five times a week.", "User goes to the gym three times a week."),
    ("contradiction", "User drives a Honda City.", "User drives a Maruti Swift."),
    ("contradiction", "User's timezone is IST.", "User's timezone is GMT."),

    # --- paraphrase / enrichment: MERGE candidates ---------------------------
    ("duplicate", "User likes black coffee.", "User prefers coffee without milk."),
    ("duplicate", "User has a dog named Rex.", "User owns a three-year-old Golden Retriever named Rex."),
    ("duplicate", "User works at Acme.", "User works at Acme as a senior backend engineer."),
    ("duplicate", "User is allergic to peanuts.", "User has a severe peanut allergy."),
    ("duplicate", "User prefers morning meetings.", "User prefers meetings before 10 AM."),
    ("duplicate", "User knows Python.", "User has eight years of Python experience."),
    ("duplicate", "User is training for a marathon.", "User is training for the Mumbai marathon in January."),
    ("duplicate", "User is vegetarian.", "User does not eat meat."),

    # --- near identical: exact-duplicate territory ---------------------------
    ("identical", "User lives in Bangalore.", "User lives in Bangalore."),
    ("identical", "User likes black coffee.", "User likes black coffee"),
    ("identical", "User works at Acme.", "user works at acme."),
]

BAND_ORDER = ["unrelated", "contradiction", "duplicate", "identical"]


async def main() -> int:
    configure_logging("ERROR", json_output=False)
    settings = get_settings()

    if settings.embedding_provider == "fake":
        print("DREAM_EMBEDDING_PROVIDER is 'fake'. Set it to azure_openai to calibrate.")
        return 1

    provider = build_embedding_provider(settings)
    print(f"Model: {provider.model_name}  ({provider.dimensions} dimensions)\n")

    try:
        # One batched call rather than 52 individual ones.
        texts: list[str] = []
        for _, a, b in PAIRS:
            texts.extend([a, b])
        vectors = await provider.embed_batch(texts)

        bands: dict[str, list[float]] = {band: [] for band in BAND_ORDER}
        rows: list[tuple[str, float, str, str]] = []

        for index, (label, a, b) in enumerate(PAIRS):
            score = cosine_similarity(vectors[index * 2], vectors[index * 2 + 1])
            bands[label].append(score)
            rows.append((label, score, a, b))

        print(f"{'band':<15}{'n':>4}{'min':>8}{'mean':>8}{'max':>8}")
        print("-" * 43)
        for band in BAND_ORDER:
            scores = bands[band]
            if not scores:
                continue
            print(
                f"{band:<15}{len(scores):>4}{min(scores):>8.3f}"
                f"{statistics.mean(scores):>8.3f}{max(scores):>8.3f}"
            )

        print("\nPer pair:")
        for label, score, a, b in sorted(rows, key=lambda r: r[1]):
            print(f"  {score:.3f}  [{label:<13}] {a[:38]:<38} | {b[:38]}")

        # --- suggested thresholds ------------------------------------------ #
        unrelated_max = max(bands["unrelated"])
        contradiction_min = min(bands["contradiction"])
        duplicate_min = min(bands["duplicate"])

        print("\n" + "=" * 60)
        print("SUGGESTED THRESHOLDS")
        print("=" * 60)

        if contradiction_min <= unrelated_max:
            # The bands overlap: no single cut-off separates them, so the
            # candidate set must lean on the same-type channel instead.
            print(
                "  WARNING: unrelated and contradiction bands overlap\n"
                f"           (unrelated peaks at {unrelated_max:.3f}, "
                f"contradiction bottoms at {contradiction_min:.3f}).\n"
                "           No threshold separates them cleanly. Keep the\n"
                "           same-type recall channel enabled — it is what makes\n"
                "           supersede work when similarity cannot discriminate.\n"
            )

        # Sit just under the weakest case each operation must still catch.
        retrieval = round(max(0.05, min(unrelated_max, contradiction_min) - 0.02), 2)
        supersede = round(max(0.05, contradiction_min - 0.05), 2)
        merge = round(max(0.10, duplicate_min - 0.05), 2)

        print(f"  retrieval.similarity_threshold      {retrieval}")
        print(f"  dream.supersede.similarity_threshold {supersede}")
        print(f"  dream.merge.similarity_threshold     {merge}")
        print(f"\n  Rationale: the weakest contradiction scored {contradiction_min:.3f}")
        print(f"             and the weakest duplicate scored {duplicate_min:.3f}.")
        print("             Thresholds sit just below those, so the hardest real")
        print("             case still reaches the reasoner.")
        print("\n  Apply in config/dream.yaml.")
        return 0
    finally:
        await provider.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
