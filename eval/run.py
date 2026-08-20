"""Evaluation runner.

    python -m eval.run                          # every suite, offline fakes
    python -m eval.run --suite supersede merge  # selected suites
    python -m eval.run --provider azure         # against a live deployment
    python -m eval.run --mode pipeline          # include extraction in the loop
    python -m eval.run --report eval/results/report.json

Each case gets a **fresh, isolated memory store**. Cases must not be able to
influence one another, or a failure early in the file quietly changes the
meaning of everything after it.

Two modes:

* ``decision`` (default) — the case's ``candidate`` text is fed straight to the
  Dream engine. Measures the decision layer alone.
* ``pipeline`` — the case's ``input`` (a raw first-person message) goes through
  extraction first. Measures what a caller actually experiences, and will score
  lower because extraction variance is included.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DATASET_DIR = Path(__file__).parent / "dataset"
SUITES = ("add", "merge", "supersede", "noop", "synthesis")


# --------------------------------------------------------------------------- #
# Dataset
# --------------------------------------------------------------------------- #


def load_suite(name: str) -> list[dict[str, Any]]:
    path = DATASET_DIR / f"{name}.jsonl"
    if not path.is_file():
        raise SystemExit(f"Dataset file not found: {path}")

    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_number}: malformed JSON — {exc}") from exc
    return cases


def matches(expectation: str | None, actual: str | None) -> bool:
    """Evaluate a content expectation against produced text.

    Expectations are matchers rather than exact strings, because the wording a
    model chooses is not the thing under test — whether the information survived
    is:

        ``contains:bangalore``          all listed terms appear
        ``all:golden retriever|rex``    every ``|``-separated term appears
        ``any:postgres|postgresql``     at least one appears
        ``absent:chennai``              none appear
    """
    if not expectation:
        return True
    if actual is None:
        return False

    text = actual.lower()
    prefix, _, payload = expectation.partition(":")
    terms = [term.strip().lower() for term in payload.split("|") if term.strip()]

    if prefix in ("contains", "all"):
        return all(term in text for term in terms)
    if prefix == "any":
        return any(term in text for term in terms)
    if prefix == "absent":
        return not any(term in text for term in terms)
    return expectation.strip().lower() in text


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass
class CaseResult:
    case_id: str
    suite: str
    expected: str
    actual: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    reason: str = ""
    produced: str | None = None


@dataclass
class SuiteResult:
    name: str
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(1 for case in self.cases if case.passed)

    @property
    def accuracy(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def action_accuracy(self) -> float:
        """How often the right action was chosen, ignoring content expectations."""
        if not self.cases:
            return 0.0
        correct = sum(1 for case in self.cases if case.expected == case.actual)
        return correct / self.total

    def confusion(self) -> dict[str, Counter]:
        matrix: dict[str, Counter] = {}
        for case in self.cases:
            matrix.setdefault(case.expected, Counter())[case.actual] += 1
        return matrix


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class Harness:
    def __init__(self, provider: str, mode: str, verbose: bool = False) -> None:
        self.provider = provider
        self.mode = mode
        self.verbose = verbose
        self._settings = self._build_settings()

    def _build_settings(self):
        from app.core.config import Settings

        overrides: dict[str, Any] = {
            "env": "eval",
            "identity_salt": "eval-salt",
            "vector_provider": "memory",
            "queue_provider": "inline",
            "mongodb_database": "memdream_eval",
            "log_level": "ERROR",
            "retrieval": {"similarity_threshold": 0.15},
            "memory": {"min_confidence": 0.4, "min_importance": 0.05},
            "dream": {
                "decision_mode": "unified",
                "supersede": {"similarity_threshold": 0.05},
                "merge": {"similarity_threshold": 0.10},
                "synthesis": {
                    "min_memories": 3,
                    "min_cluster_size": 3,
                    # High enough that three unrelated facts do not group. The
                    # bag-of-words fake embedder scores unrelated short texts
                    # around 0.1-0.2, so a lower cut-off clusters anything.
                    "cluster_similarity_threshold": 0.22,
                    "min_pattern_confidence": 0.4,
                    "min_pattern_importance": 0.2,
                    "max_clusters_per_run": 5,
                },
            },
        }

        if self.provider == "azure":
            settings = Settings(llm_provider="azure_openai", embedding_provider="azure_openai", **overrides)
            settings.require_azure_openai()
            return settings
        return Settings(llm_provider="fake", embedding_provider="fake", **overrides)

    async def _container(self):
        """A container with a private, empty store."""
        from mongomock_motor import AsyncMongoMockClient

        from app.container import DreamContainer
        from app.providers.queue.inline_queue import InlineJobQueue
        from app.providers.vector_store.memory_store import InMemoryVectorStore
        from app.repositories.mongo import MongoDatabase

        database = MongoDatabase(AsyncMongoMockClient(tz_aware=True), "memdream_eval")
        return await DreamContainer.create(
            self._settings,
            database=database,
            vectors=InMemoryVectorStore(),
            queue=InlineJobQueue(),
            ensure_schema=False,
        )

    @staticmethod
    def _scope():
        from app.models.scope import MemoryScope

        return MemoryScope(client_id="eval_client", bot_id="eval_bot", user_id="eval_user")

    async def _seed(self, container, scope, entries: list[dict[str, Any]]):
        """Persist the case's pre-existing memories, in order."""
        from app.models.memory import Memory

        stored = []
        for entry in entries:
            content = entry["content"] if isinstance(entry, dict) else str(entry)
            extra = entry if isinstance(entry, dict) else {}
            memory = Memory(
                client_id=scope.client_id,
                bot_id=scope.bot_id,
                user_id=scope.user_id,
                content=content,
                memory_type=extra.get("memoryType", "fact"),
                importance=extra.get("importance", 0.6),
                confidence=extra.get("confidence", 0.9),
            )
            if "temporalNature" in extra:
                from app.models.enums import TemporalNature

                memory.temporal_nature = TemporalNature(extra["temporalNature"])
            memory.embedding = await container.embeddings.embed(content)
            memory.embedding_model = container.embeddings.model_name
            await container.lifecycle.activate(scope, memory)
            stored.append(memory)
        return stored

    # --- decision suites ---------------------------------------------------- #

    async def run_decision_case(self, suite: str, case: dict[str, Any]) -> CaseResult:
        from app.models.dream import ExtractedMemory

        container = await self._container()
        scope = self._scope()
        started = time.perf_counter()
        failures: list[str] = []

        try:
            seeded = await self._seed(container, scope, case.get("existingMemories", []))

            if self.mode == "pipeline" and case.get("input"):
                outcome = await container.memory_service.add(scope, case["input"])
                result = outcome.primary
                if result is None:
                    return CaseResult(
                        case_id=case["id"],
                        suite=suite,
                        expected=case["expectedAction"],
                        actual="NO_EXTRACTION",
                        passed=case["expectedAction"] == "NOOP",
                        failures=["extraction produced no candidate"],
                        latency_ms=(time.perf_counter() - started) * 1000.0,
                    )
            else:
                candidate = ExtractedMemory(
                    content=case.get("candidate") or case["input"],
                    memory_type=case.get("memoryType", "fact"),
                    importance=case.get("importance", 0.6),
                    confidence=case.get("confidence", 0.95),
                )
                result = await container.dream_engine.process(scope, candidate)

            expected = case["expectedAction"]
            actual = result.action.value
            if actual != expected:
                failures.append(f"expected {expected}, got {actual}")

            # Targets are given as indices into `existingMemories`.
            expected_targets = case.get("expectedTargets")
            if expected_targets is not None and actual == expected:
                want = {seeded[i].memory_id for i in expected_targets if i < len(seeded)}
                got = set(result.superseded_memory_ids) | set(result.merged_memory_ids)
                if want != got:
                    failures.append(
                        f"targets: expected indices {expected_targets}, got {len(got)} id(s)"
                    )

            produced = result.memory.content if result.memory else None
            if not matches(case.get("expectedCanonical"), produced):
                failures.append(f"content {case['expectedCanonical']!r} not satisfied by {produced!r}")

            return CaseResult(
                case_id=case["id"],
                suite=suite,
                expected=expected,
                actual=actual,
                passed=not failures,
                failures=failures,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                reason=result.reason,
                produced=produced,
            )
        finally:
            await container.vectors.close()

    # --- synthesis suite ------------------------------------------------------ #

    async def run_synthesis_case(self, case: dict[str, Any]) -> CaseResult:
        container = await self._container()
        scope = self._scope()
        started = time.perf_counter()
        failures: list[str] = []

        try:
            await self._seed(container, scope, case.get("memories", []))

            # A case may declare patterns that already exist, to test idempotency.
            for existing in case.get("existingPatterns", []):
                from app.models.memory import Memory, new_memory_id

                pattern = Memory(
                    memory_id=new_memory_id("pattern"),
                    client_id=scope.client_id,
                    bot_id=scope.bot_id,
                    user_id=scope.user_id,
                    content=existing["content"],
                    memory_type="pattern",
                    explicit=False,
                    source_memory_ids=[],
                )
                pattern.embedding = await container.embeddings.embed(pattern.content)
                await container.lifecycle.activate(scope, pattern)

            before = len(await container.memories.find_patterns(scope))
            job = await container.dream_service.request_synthesis(scope, wait=True)
            patterns = await container.memories.find_patterns(scope)
            created = len(patterns) - before

            expected = case["expectedAction"]
            actual = "SYNTHESIZE" if created > 0 else "NOOP"
            if actual != expected:
                failures.append(f"expected {expected}, got {actual} (job: {job.error or job.status.value})")

            produced = None
            if created > 0:
                newest = max(patterns, key=lambda p: p.created_at)
                produced = newest.content
                if not matches(case.get("expectedPattern"), produced):
                    failures.append(f"pattern {case['expectedPattern']!r} not satisfied")
                minimum = case.get("minSources", 3)
                if len(newest.source_memory_ids) < minimum:
                    failures.append(
                        f"expected at least {minimum} source ids, got {len(newest.source_memory_ids)}"
                    )

            # Idempotency: a second identical run must not add patterns.
            if case.get("checkIdempotent", True) and created > 0:
                await container.dream_service.request_synthesis(scope, wait=True, force=True)
                after = len(await container.memories.find_patterns(scope))
                if after != len(patterns):
                    failures.append(f"not idempotent: {len(patterns)} -> {after} patterns")

            return CaseResult(
                case_id=case["id"],
                suite="synthesis",
                expected=expected,
                actual=actual,
                passed=not failures,
                failures=failures,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                produced=produced,
            )
        finally:
            await container.vectors.close()

    async def run_suite(self, name: str) -> SuiteResult:
        cases = load_suite(name)
        result = SuiteResult(name=name)

        for index, case in enumerate(cases, 1):
            if name == "synthesis":
                case_result = await self.run_synthesis_case(case)
            else:
                case_result = await self.run_decision_case(name, case)
            result.cases.append(case_result)

            if self.verbose or not case_result.passed:
                mark = "PASS" if case_result.passed else "FAIL"
                print(f"  [{mark}] {case_result.case_id}", file=sys.stderr)
                for failure in case_result.failures:
                    print(f"         {failure}", file=sys.stderr)
            elif index % 10 == 0:
                print(f"  ...{index}/{len(cases)}", file=sys.stderr)

        return result


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def print_report(results: list[SuiteResult], elapsed: float) -> None:
    print("\n" + "=" * 68)
    print("DREAM MEMORY — EVALUATION REPORT")
    print("=" * 68)
    print(f"{'suite':<14}{'cases':>7}{'passed':>8}{'overall':>10}{'action':>10}")
    print("-" * 68)

    total = passed = 0
    for suite in results:
        total += suite.total
        passed += suite.passed
        print(
            f"{suite.name:<14}{suite.total:>7}{suite.passed:>8}"
            f"{suite.accuracy:>9.1%}{suite.action_accuracy:>10.1%}"
        )

    print("-" * 68)
    overall = passed / total if total else 0.0
    print(f"{'TOTAL':<14}{total:>7}{passed:>8}{overall:>9.1%}")
    print(f"\nCompleted in {elapsed:.1f}s")

    # The confusion matrix is where the useful signal is: which action is being
    # chosen instead of the right one.
    print("\nConfusion (expected -> actual):")
    combined: dict[str, Counter] = {}
    for suite in results:
        for expected, counts in suite.confusion().items():
            combined.setdefault(expected, Counter()).update(counts)
    for expected in sorted(combined):
        breakdown = ", ".join(
            f"{actual}={count}" for actual, count in combined[expected].most_common()
        )
        print(f"  {expected:<12} {breakdown}")

    failures = [case for suite in results for case in suite.cases if not case.passed]
    if failures:
        print(f"\n{len(failures)} failing case(s):")
        for case in failures[:25]:
            print(f"  {case.case_id:<16} {case.expected} -> {case.actual}")
            for failure in case.failures:
                print(f"    {failure}")
        if len(failures) > 25:
            print(f"  ... and {len(failures) - 25} more")


def write_report(results: list[SuiteResult], path: Path, provider: str, mode: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "provider": provider,
        "mode": mode,
        "suites": [
            {
                "name": suite.name,
                "total": suite.total,
                "passed": suite.passed,
                "accuracy": round(suite.accuracy, 4),
                "actionAccuracy": round(suite.action_accuracy, 4),
                "cases": [
                    {
                        "id": case.case_id,
                        "expected": case.expected,
                        "actual": case.actual,
                        "passed": case.passed,
                        "failures": case.failures,
                        "latencyMs": round(case.latency_ms, 2),
                        "produced": case.produced,
                        "reason": case.reason,
                    }
                    for case in suite.cases
                ],
            }
            for suite in results
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nReport written to {path}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


async def _run(args: argparse.Namespace) -> int:
    from app.core.logging import configure_logging

    # Quiet by default: 250 cases each emitting activation logs buries the report.
    configure_logging("DEBUG" if args.verbose else "ERROR", json_output=False)

    harness = Harness(provider=args.provider, mode=args.mode, verbose=args.verbose)
    started = time.perf_counter()

    results: list[SuiteResult] = []
    for name in args.suite:
        print(f"\nRunning suite: {name}", file=sys.stderr)
        results.append(await harness.run_suite(name))

    elapsed = time.perf_counter() - started
    print_report(results, elapsed)

    if args.report:
        write_report(results, Path(args.report), args.provider, args.mode)

    total = sum(suite.total for suite in results)
    passed = sum(suite.passed for suite in results)
    accuracy = passed / total if total else 0.0
    if accuracy < args.threshold:
        print(f"\nFAILED: accuracy {accuracy:.1%} is below the {args.threshold:.1%} threshold")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the Dream decision layer")
    parser.add_argument("--suite", nargs="+", choices=SUITES, default=list(SUITES))
    parser.add_argument(
        "--provider",
        choices=("fake", "azure"),
        default="fake",
        help="fake runs offline and free; azure uses your configured deployment",
    )
    parser.add_argument(
        "--mode",
        choices=("decision", "pipeline"),
        default="decision",
        help="decision isolates the Dream layer; pipeline includes extraction",
    )
    parser.add_argument("--report", help="write a JSON report to this path")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.0,
        help="exit non-zero below this overall accuracy (for CI)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
