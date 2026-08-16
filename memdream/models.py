"""Lightweight response models.

Plain dataclasses rather than Pydantic: the SDK should be installable next to an
agent framework without dragging in a validation library or forcing a Pydantic
major version on the caller. Every model keeps its `raw` payload, so a field
added by a newer server is still reachable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _parse_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return value if isinstance(value, datetime) else None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@dataclass
class MemoryRecord:
    """One memory as returned by the API."""

    memory_id: str
    content: str
    memory_type: str
    status: str
    importance: float = 0.5
    confidence: float = 0.8
    created_at: datetime | None = None
    updated_at: datetime | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    superseded_by: str | None = None
    merged_into: str | None = None
    source_memory_ids: list[str] = field(default_factory=list)
    #: Present on search results only.
    score: float | None = None
    score_breakdown: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryRecord:
        return cls(
            memory_id=data.get("memoryId", ""),
            content=data.get("content", ""),
            memory_type=data.get("memoryType", "other"),
            status=data.get("status", "active"),
            importance=data.get("importance", 0.5),
            confidence=data.get("confidence", 0.8),
            created_at=_parse_datetime(data.get("createdAt")),
            updated_at=_parse_datetime(data.get("updatedAt")),
            valid_from=_parse_datetime(data.get("validFrom")),
            valid_until=_parse_datetime(data.get("validUntil")),
            superseded_by=data.get("supersededBy"),
            merged_into=data.get("mergedInto"),
            source_memory_ids=data.get("sourceMemoryIds", []) or [],
            score=data.get("score"),
            score_breakdown=data.get("scoreBreakdown", {}) or {},
            metadata=data.get("metadata", {}) or {},
            raw=data,
        )


@dataclass
class DecisionResult:
    """What Dream did with one extracted memory."""

    action: str
    memory_id: str | None = None
    content: str | None = None
    reason: str = ""
    superseded_memory_ids: list[str] = field(default_factory=list)
    merged_memory_ids: list[str] = field(default_factory=list)
    operation_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DecisionResult:
        return cls(
            action=data.get("action", "NOOP"),
            memory_id=data.get("memoryId"),
            content=data.get("content"),
            reason=data.get("reason", ""),
            superseded_memory_ids=data.get("supersededMemoryIds", []) or [],
            merged_memory_ids=data.get("mergedMemoryIds", []) or [],
            operation_id=data.get("operationId"),
        )


@dataclass
class AddResult:
    """The result of remembering one message.

    `action` and `memory_id` mirror the most significant decision; `results`
    holds every decision, since one message can carry several facts.
    """

    action: str
    memory_id: str | None = None
    superseded_memory_ids: list[str] = field(default_factory=list)
    merged_memory_ids: list[str] = field(default_factory=list)
    results: list[DecisionResult] = field(default_factory=list)
    extracted_count: int = 0
    request_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def remembered(self) -> bool:
        """True when the store changed as a result of this call."""
        return any(result.action != "NOOP" for result in self.results)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AddResult:
        return cls(
            action=data.get("action", "NOOP"),
            memory_id=data.get("memoryId"),
            superseded_memory_ids=data.get("supersededMemoryIds", []) or [],
            merged_memory_ids=data.get("mergedMemoryIds", []) or [],
            results=[DecisionResult.from_dict(item) for item in data.get("results", [])],
            extracted_count=data.get("extractedCount", 0),
            request_id=data.get("requestId"),
            raw=data,
        )


@dataclass
class SearchResult:
    """Search results, iterable and indexable directly."""

    memories: list[MemoryRecord] = field(default_factory=list)
    count: int = 0
    request_id: str | None = None

    def __iter__(self):
        return iter(self.memories)

    def __len__(self) -> int:
        return len(self.memories)

    def __getitem__(self, index):
        return self.memories[index]

    def as_context(self, separator: str = "\n") -> str:
        """Render memories as plain lines for a prompt.

        The most common thing a caller does with search results, so it ships
        rather than being reimplemented in every integration.
        """
        return separator.join(f"- {memory.content}" for memory in self.memories)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SearchResult:
        return cls(
            memories=[MemoryRecord.from_dict(item) for item in data.get("memories", [])],
            count=data.get("count", 0),
            request_id=data.get("requestId"),
        )


@dataclass
class RelatedMemory:
    memory_id: str
    relation_type: str
    content: str
    status: str
    memory_type: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RelatedMemory:
        return cls(
            memory_id=data.get("memoryId", ""),
            relation_type=data.get("relationType", ""),
            content=data.get("content", ""),
            status=data.get("status", ""),
            memory_type=data.get("memoryType", ""),
        )


@dataclass
class MemoryDetail:
    memory: MemoryRecord
    relationships: list[RelatedMemory] = field(default_factory=list)
    source_memories: list[RelatedMemory] = field(default_factory=list)
    derived_memories: list[RelatedMemory] = field(default_factory=list)
    superseded_by: str | None = None
    merged_into: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryDetail:
        return cls(
            memory=MemoryRecord.from_dict(data.get("memory", {})),
            relationships=[RelatedMemory.from_dict(r) for r in data.get("relationships", [])],
            source_memories=[RelatedMemory.from_dict(r) for r in data.get("sourceMemories", [])],
            derived_memories=[RelatedMemory.from_dict(r) for r in data.get("derivedMemories", [])],
            superseded_by=data.get("supersededBy"),
            merged_into=data.get("mergedInto"),
        )


@dataclass
class DreamJob:
    job_id: str
    status: str
    patterns_created: int = 0
    patterns_updated: int = 0
    patterns_skipped: int = 0
    memories_analyzed: int = 0
    clusters_evaluated: int = 0
    created_pattern_ids: list[str] = field(default_factory=list)
    error: str | None = None
    duration_ms: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_finished(self) -> bool:
        return self.status in ("completed", "failed", "skipped")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DreamJob:
        return cls(
            job_id=data.get("jobId", ""),
            status=data.get("status", "queued"),
            patterns_created=data.get("patternsCreated") or 0,
            patterns_updated=data.get("patternsUpdated") or 0,
            patterns_skipped=data.get("patternsSkipped") or 0,
            memories_analyzed=data.get("memoriesAnalyzed") or 0,
            clusters_evaluated=data.get("clustersEvaluated") or 0,
            created_pattern_ids=data.get("createdPatternIds", []) or [],
            error=data.get("error"),
            duration_ms=data.get("durationMs"),
            raw=data,
        )
