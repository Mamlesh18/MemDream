"""Schemas for everything the LLM produces, plus audit and job records.

The models here are the contract boundary with the language model. The LLM
*proposes* — it returns one of these shapes and nothing else. The backend
validates the proposal against the memories it actually supplied, then executes
the state transition itself. A model can therefore return a nonsense id or an
out-of-range score without corrupting anything: validation rejects it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import (
    DreamAction,
    JobStatus,
    MemoryType,
    SynthesisAction,
    TemporalNature,
)
from app.models.memory import _to_camel, utcnow


def _parse_llm_datetime(value: Any) -> datetime | None:
    """Best-effort parse of a date the model emitted.

    Models reliably produce ISO dates when asked, but occasionally emit ``null``,
    an empty string, or prose like "last month". Anything unparseable becomes
    ``None`` — an absent validity bound is correct and harmless, whereas a
    hallucinated one would silently distort temporal queries.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text or text.lower() in {"null", "none", "unknown", "n/a"}:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # A bare year, or "2026-07" — good enough to anchor a validity window.
        for fmt in ("%Y-%m", "%Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class _LLMModel(BaseModel):
    """Base for model-produced payloads: camelCase in, tolerant of extras."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="ignore",
    )


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


class ExtractedMemory(_LLMModel):
    """One candidate long-term memory pulled out of a user message."""

    content: str
    memory_type: str = MemoryType.OTHER.value
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    explicit: bool = True
    temporal_nature: TemporalNature = TemporalNature.CURRENT
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    #: The extractor flags candidates that carry PII, credentials, health or
    #: financial detail; `MemoryPolicy` decides what to do about it.
    sensitive: bool = False

    @field_validator("valid_from", "valid_until", mode="before")
    @classmethod
    def _parse_dates(cls, value: Any) -> Any:
        return _parse_llm_datetime(value)

    @field_validator("memory_type")
    @classmethod
    def _normalise_type(cls, value: str) -> str:
        return (value or MemoryType.OTHER.value).strip().lower() or MemoryType.OTHER.value

    @field_validator("content")
    @classmethod
    def _strip(cls, value: str) -> str:
        return (value or "").strip()


class ExtractionResult(_LLMModel):
    """The extractor's full response for one prompt."""

    has_memory: bool = False
    memories: list[ExtractedMemory] = Field(default_factory=list)
    reason: str = ""


# --------------------------------------------------------------------------- #
# Real-time Dream decision
# --------------------------------------------------------------------------- #


class ProposedMemory(_LLMModel):
    """The memory text a decision wants to end up active.

    For ADD it is the extracted candidate; for SUPERSEDE the new fact; for MERGE
    the canonical text that subsumes both inputs.
    """

    content: str
    memory_type: str = MemoryType.OTHER.value
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    temporal_nature: TemporalNature = TemporalNature.CURRENT
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @field_validator("valid_from", "valid_until", mode="before")
    @classmethod
    def _parse_dates(cls, value: Any) -> Any:
        return _parse_llm_datetime(value)

    @field_validator("memory_type")
    @classmethod
    def _normalise_type(cls, value: str) -> str:
        return (value or MemoryType.OTHER.value).strip().lower() or MemoryType.OTHER.value


class DreamDecision(_LLMModel):
    """The reasoner's recommendation for one candidate memory.

    ``target_memory_ids`` must reference memories that were supplied in the
    prompt; the engine drops any id it does not recognise rather than trusting it.
    """

    action: DreamAction = DreamAction.ADD
    reason: str = ""
    memory: ProposedMemory | None = None
    target_memory_ids: list[str] = Field(default_factory=list)
    #: Optional per-target validUntil, letting the model close out the old fact's
    #: validity window at the moment the new one begins.
    superseded_valid_until: datetime | None = None

    @field_validator("superseded_valid_until", mode="before")
    @classmethod
    def _parse_date(cls, value: Any) -> Any:
        return _parse_llm_datetime(value)

    @field_validator("target_memory_ids", mode="before")
    @classmethod
    def _coerce_ids(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value


# --------------------------------------------------------------------------- #
# Synthesis
# --------------------------------------------------------------------------- #


class PatternProposal(_LLMModel):
    """A higher-order pattern the synthesiser believes the evidence supports."""

    content: str
    memory_type: str = MemoryType.PATTERN.value
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    importance: float = Field(default=0.6, ge=0.0, le=1.0)
    source_memory_ids: list[str] = Field(default_factory=list)

    @field_validator("source_memory_ids", mode="before")
    @classmethod
    def _coerce_ids(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value


class SynthesisDecision(_LLMModel):
    action: SynthesisAction = SynthesisAction.NOOP
    reason: str = ""
    pattern: PatternProposal | None = None


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


class DreamOperation(BaseModel):
    """One audit row. Written for every Dream decision, NOOPs included.

    NOOPs matter as much as writes here: "why did nothing happen?" is the most
    common question when tuning thresholds.
    """

    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True)

    operation_id: str = Field(default_factory=lambda: f"op_{uuid.uuid4().hex[:24]}")

    client_id: str
    bot_id: str
    user_id: str

    action: str
    #: The memory that ended up active as a result (None for NOOP).
    memory_id: str | None = None
    target_memory_ids: list[str] = Field(default_factory=list)

    reason: str = ""
    #: Verbatim input that triggered the operation, for reproducing a decision.
    input_content: str | None = None
    #: Ids the reasoner was shown, so a bad decision can be replayed exactly.
    candidate_memory_ids: list[str] = Field(default_factory=list)

    model: str | None = None
    provider: str | None = None
    confidence: float | None = None

    request_id: str | None = None
    job_id: str | None = None
    latency_ms: float | None = None
    token_usage: dict[str, int] = Field(default_factory=dict)
    #: Set when the backend overruled or trimmed the model's proposal.
    validation_notes: list[str] = Field(default_factory=list)

    timestamp: datetime = Field(default_factory=utcnow)

    def to_document(self) -> dict[str, Any]:
        doc = self.model_dump(by_alias=True)
        doc["_id"] = doc.pop("operationId")
        return doc

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> DreamOperation:
        data = dict(document)
        if "_id" in data:
            data["operationId"] = data.pop("_id")
        return cls.model_validate(data)


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


class DreamJob(BaseModel):
    """A queued or completed synthesis run."""

    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True)

    job_id: str = Field(default_factory=lambda: f"dream_job_{uuid.uuid4().hex[:16]}")

    client_id: str
    bot_id: str
    user_id: str

    status: JobStatus = JobStatus.QUEUED
    job_type: str = "synthesis"

    memories_analyzed: int = 0
    clusters_evaluated: int = 0
    patterns_created: int = 0
    patterns_updated: int = 0
    patterns_skipped: int = 0
    noop_decisions: int = 0

    error: str | None = None
    #: Ids of the patterns this run produced, so callers can fetch them directly.
    created_pattern_ids: list[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: float | None = None

    requested_by: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_document(self) -> dict[str, Any]:
        doc = self.model_dump(by_alias=True)
        doc["_id"] = doc.pop("jobId")
        doc["status"] = self.status.value
        return doc

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> DreamJob:
        data = dict(document)
        if "_id" in data:
            data["jobId"] = data.pop("_id")
        return cls.model_validate(data)

    def public_dump(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")
