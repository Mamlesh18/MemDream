"""A tiny in-process metrics registry.

Deliberately dependency-free: counters and latency histograms live in memory and
are exposed on ``GET /v1/metrics``. When ``DREAM_OTEL_ENABLED=true`` and the
OpenTelemetry extra is installed, the same instruments are mirrored to OTel — so
enabling real telemetry never requires touching call sites.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

# --- metric names ----------------------------------------------------------- #

MEMORIES_ADDED = "memories_added"
MEMORIES_MERGED = "memories_merged"
MEMORIES_SUPERSEDED = "memories_superseded"
PATTERNS_CREATED = "patterns_created"
PATTERNS_UPDATED = "patterns_updated"
NOOP_DECISIONS = "noop_decisions"
LLM_CALLS = "llm_calls"
LLM_ERRORS = "llm_errors"
EMBEDDING_CALLS = "embedding_calls"
#: Messages the triage gate answered without an LLM call, by reason.
#: The gap between this and `llm_calls` is what the gate is saving you.
EXTRACTION_SKIPPED = "extraction_skipped"
EMBEDDING_ERRORS = "embedding_errors"
VECTOR_SEARCHES = "vector_searches"
SYNTHESIS_RUNS = "synthesis_runs"

# --- tokens ---------------------------------------------------------------- #
#
# Split rather than aggregated, because input and output are priced differently
# — often by 4x or more — so a single "total tokens" number cannot be turned
# into a cost. Every one of these is labelled with the operation that spent it,
# which is what makes "what does one add actually cost?" answerable.

LLM_PROMPT_TOKENS = "llm_prompt_tokens"
LLM_COMPLETION_TOKENS = "llm_completion_tokens"
LLM_TOKENS = "llm_tokens"
EMBEDDING_TOKENS = "embedding_tokens"

LLM_LATENCY = "llm_latency_ms"
EMBEDDING_LATENCY = "embedding_latency_ms"
VECTOR_SEARCH_LATENCY = "vector_search_latency_ms"
RETRIEVAL_LATENCY = "retrieval_latency_ms"
DREAM_LATENCY = "dream_latency_ms"
SYNTHESIS_DURATION = "synthesis_duration_ms"

_LabelKey = tuple[tuple[str, str], ...]

#: The operation currently being performed, for labelling provider metrics.
#:
#: A context variable rather than a parameter on `generate_json`, so the provider
#: interface stays about *calling a model* and does not grow a field describing
#: why. Call sites wrap themselves in `operation()`; providers read it. Same
#: approach as `request_id` in `app.core.logging`.
_operation: ContextVar[str] = ContextVar("dream_metric_operation", default="unattributed")

#: Every operation that spends tokens. Kept as constants so a typo at a call
#: site cannot silently create a new bucket that never gets reported.
OP_EXTRACT = "extract"        # pulling durable facts out of a message
OP_DECIDE = "decide"          # ADD / SUPERSEDE / MERGE / NOOP
OP_SYNTHESIZE = "synthesize"  # pattern generation
OP_REMEMBER = "remember"      # embedding on the write path
OP_SEARCH = "search"          # embedding a search query
OP_MANUAL = "manual"          # embedding a manually written memory
OP_REBUILD = "rebuild"        # re-embedding during an index rebuild

ALL_OPERATIONS = (
    OP_EXTRACT, OP_DECIDE, OP_SYNTHESIZE,
    OP_REMEMBER, OP_SEARCH, OP_MANUAL, OP_REBUILD,
)


@contextmanager
def operation(name: str) -> Iterator[None]:
    """Attribute every provider call inside this block to ``name``."""
    token = _operation.set(name)
    try:
        yield
    finally:
        _operation.reset(token)


def current_operation() -> str:
    return _operation.get()


class _Histogram:
    """Count / sum / min / max plus coarse buckets.

    Full quantiles would mean retaining samples; for operational purposes the
    bucket boundaries below answer "is this getting slow?" at a fraction of the
    memory cost.
    """

    __slots__ = ("count", "total", "minimum", "maximum", "buckets")

    BOUNDS = (5.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0, 10000.0)

    def __init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.minimum = float("inf")
        self.maximum = 0.0
        self.buckets = [0] * (len(self.BOUNDS) + 1)

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)
        for index, bound in enumerate(self.BOUNDS):
            if value <= bound:
                self.buckets[index] += 1
                return
        self.buckets[-1] += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "sum": round(self.total, 3),
            "avg": round(self.total / self.count, 3) if self.count else 0.0,
            "min": round(self.minimum, 3) if self.count else 0.0,
            "max": round(self.maximum, 3),
            "buckets": {
                **{f"<={bound}": self.buckets[i] for i, bound in enumerate(self.BOUNDS)},
                "+Inf": self.buckets[-1],
            },
        }


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, _LabelKey], float] = {}
        self._histograms: dict[tuple[str, _LabelKey], _Histogram] = {}
        self._otel_meter: Any = None
        self._otel_instruments: dict[str, Any] = {}

    # --- OTel bridge -------------------------------------------------------- #

    def enable_otel(self, service_name: str = "dream-memory") -> bool:
        """Attach an OTel meter. Returns False when the extra is not installed."""
        try:
            from opentelemetry import metrics as otel_metrics
        except ImportError:
            return False
        self._otel_meter = otel_metrics.get_meter(service_name)
        return True

    def _otel_counter(self, name: str) -> Any:
        if self._otel_meter is None:
            return None
        instrument = self._otel_instruments.get(f"c:{name}")
        if instrument is None:
            instrument = self._otel_meter.create_counter(f"dream.{name}")
            self._otel_instruments[f"c:{name}"] = instrument
        return instrument

    def _otel_histogram(self, name: str) -> Any:
        if self._otel_meter is None:
            return None
        instrument = self._otel_instruments.get(f"h:{name}")
        if instrument is None:
            instrument = self._otel_meter.create_histogram(f"dream.{name}", unit="ms")
            self._otel_instruments[f"h:{name}"] = instrument
        return instrument

    # --- recording ---------------------------------------------------------- #

    @staticmethod
    def _key(labels: dict[str, str] | None) -> _LabelKey:
        if not labels:
            return ()
        return tuple(sorted((str(k), str(v)) for k, v in labels.items() if v is not None))

    def increment(
        self, name: str, value: float = 1.0, labels: dict[str, str] | None = None
    ) -> None:
        key = (name, self._key(labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value
        instrument = self._otel_counter(name)
        if instrument is not None:
            instrument.add(value, labels or {})

    def observe(
        self, name: str, value_ms: float, labels: dict[str, str] | None = None
    ) -> None:
        key = (name, self._key(labels))
        with self._lock:
            histogram = self._histograms.get(key)
            if histogram is None:
                histogram = _Histogram()
                self._histograms[key] = histogram
            histogram.observe(value_ms)
        instrument = self._otel_histogram(name)
        if instrument is not None:
            instrument.record(value_ms, labels or {})

    @contextmanager
    def timer(self, name: str, labels: dict[str, str] | None = None) -> Iterator[None]:
        """Time a block in milliseconds, recording even when it raises."""
        started = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, (time.perf_counter() - started) * 1000.0, labels)

    # --- reading ------------------------------------------------------------ #

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters: dict[str, Any] = {}
            for (name, labels), value in self._counters.items():
                counters.setdefault(name, {})[self._label_str(labels)] = value
            histograms: dict[str, Any] = {}
            for (name, labels), histogram in self._histograms.items():
                histograms.setdefault(name, {})[self._label_str(labels)] = histogram.to_dict()
        return {"counters": counters, "histograms": histograms}

    @staticmethod
    def _label_str(labels: _LabelKey) -> str:
        if not labels:
            return "_"
        return ",".join(f"{k}={v}" for k, v in labels)

    # --- token accounting ---------------------------------------------------- #

    def _sum_by_operation(self, name: str) -> dict[str, float]:
        """Total a counter, grouped by its `operation` label."""
        totals: dict[str, float] = {}
        with self._lock:
            for (metric, labels), value in self._counters.items():
                if metric != name:
                    continue
                op = dict(labels).get("operation", "unattributed")
                totals[op] = totals.get(op, 0.0) + value
        return totals

    def token_report(self, pricing: Any = None) -> dict[str, Any]:
        """Token usage broken down by operation, with optional cost estimates.

        Input and output are reported separately because they are priced
        separately — usually with output several times more expensive — so an
        aggregate token count cannot be converted into a cost.
        """
        prompt = self._sum_by_operation(LLM_PROMPT_TOKENS)
        completion = self._sum_by_operation(LLM_COMPLETION_TOKENS)
        embedding = self._sum_by_operation(EMBEDDING_TOKENS)
        calls = self._sum_by_operation(LLM_CALLS)
        embed_calls = self._sum_by_operation(EMBEDDING_CALLS)

        operations = sorted(set(prompt) | set(completion) | set(embedding))
        rows: list[dict[str, Any]] = []

        for op in operations:
            row: dict[str, Any] = {
                "operation": op,
                "llmCalls": int(calls.get(op, 0)),
                "embeddingCalls": int(embed_calls.get(op, 0)),
                "inputTokens": int(prompt.get(op, 0)),
                "outputTokens": int(completion.get(op, 0)),
                "embeddingTokens": int(embedding.get(op, 0)),
            }
            row["totalTokens"] = (
                row["inputTokens"] + row["outputTokens"] + row["embeddingTokens"]
            )
            if pricing is not None:
                row["estimatedCost"] = round(
                    pricing.cost_for(
                        input_tokens=row["inputTokens"],
                        output_tokens=row["outputTokens"],
                        embedding_tokens=row["embeddingTokens"],
                    ),
                    6,
                )
            rows.append(row)

        totals = {
            "inputTokens": sum(r["inputTokens"] for r in rows),
            "outputTokens": sum(r["outputTokens"] for r in rows),
            "embeddingTokens": sum(r["embeddingTokens"] for r in rows),
            "llmCalls": sum(r["llmCalls"] for r in rows),
            "embeddingCalls": sum(r["embeddingCalls"] for r in rows),
        }
        totals["totalTokens"] = (
            totals["inputTokens"] + totals["outputTokens"] + totals["embeddingTokens"]
        )

        report: dict[str, Any] = {"byOperation": rows, "totals": totals}

        if pricing is not None:
            totals["estimatedCost"] = round(
                pricing.cost_for(
                    input_tokens=totals["inputTokens"],
                    output_tokens=totals["outputTokens"],
                    embedding_tokens=totals["embeddingTokens"],
                ),
                6,
            )
            report["pricing"] = {
                "currency": pricing.currency,
                "llmInputPer1M": pricing.llm_input_per_1m,
                "llmOutputPer1M": pricing.llm_output_per_1m,
                "embeddingPer1M": pricing.embedding_per_1m,
                "note": (
                    "Estimates from configured rates, not billed amounts. "
                    "Set them under `pricing` in config/dream.yaml to match your "
                    "deployment and region."
                ),
            }
        return report

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()


metrics = MetricsRegistry()
