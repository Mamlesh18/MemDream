"""The canonical memory record and the edges between records.

Mongo is the source of truth. The vector index holds a *copy* of the embedding
and can be rebuilt from these documents at any time, which is why the embedding
is persisted here as well.

Wire format is camelCase (matching the public API); Python is snake_case. The
alias generator below bridges the two, and `populate_by_name` means both spellings
construct successfully.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models.scope import MemoryScope

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import MemoryStatus, MemoryType, RelationType, TemporalNature

_WHITESPACE = re.compile(r"\s+")
_TRAILING_PUNCT = re.compile(r"[.!?,;:\s]+$")


def utcnow() -> datetime:
    """Timezone-aware UTC now, truncated to milliseconds.

    Truncation keeps round-trips through BSON (millisecond precision) stable, so
    a document read back compares equal to the one written.
    """
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def _to_camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(word.capitalize() for word in rest)


def new_memory_id(memory_type: str | None = None) -> str:
    """``pat_…`` for patterns, ``mem_…`` for everything else.

    The prefix is cosmetic — nothing branches on it — but it makes logs and audit
    trails readable at a glance.
    """
    prefix = "pat" if memory_type == MemoryType.PATTERN.value else "mem"
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def normalize_content(content: str) -> str:
    """Fold a memory's text to a comparable form.

    Used for exact-duplicate detection, which must catch "I prefer black coffee."
    versus "i prefer black coffee" without an LLM call. Semantic duplicates are
    a separate problem, handled by the merge engine.
    """
    text = unicodedata.normalize("NFKC", content or "").strip().lower()
    text = _WHITESPACE.sub(" ", text)
    return _TRAILING_PUNCT.sub("", text)


def content_fingerprint(content: str) -> str:
    """Stable hash of the normalised content."""
    return hashlib.sha256(normalize_content(content).encode("utf-8")).hexdigest()[:32]


class Memory(BaseModel):
    """A single long-term memory."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        use_enum_values=False,
        ser_json_timedelta="iso8601",
    )

    memory_id: str = Field(default_factory=new_memory_id)

    # --- tenancy (never derived from model output) ------------------------ #
    #
    # `user_id` is the isolation boundary; the rest are optional dimensions.
    # `scope_key` is the digest actually filtered on — see models/scope.py for
    # why filtering uses one indexed key rather than a multi-field predicate.
    user_id: str
    agent_id: str | None = None
    client_id: str | None = None
    namespace: dict[str, str] = Field(default_factory=dict)
    scope_key: str = ""
    phone_number_hash: str | None = None

    # --- content ---------------------------------------------------------- #
    content: str
    memory_type: str = MemoryType.OTHER.value
    status: MemoryStatus = MemoryStatus.ACTIVE

    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)

    #: True when the memory restates something the user said outright; False when
    #: the engine inferred it. Inference should never masquerade as testimony.
    explicit: bool = True
    temporal_nature: TemporalNature = TemporalNature.CURRENT

    # --- time ------------------------------------------------------------- #
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    #: When the *fact* started being true — distinct from when we learned it.
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    # --- lifecycle pointers ------------------------------------------------ #
    superseded_by: str | None = None
    merged_into: str | None = None

    # --- provenance -------------------------------------------------------- #
    source_memory_ids: list[str] = Field(default_factory=list)
    source_message_ids: list[str] = Field(default_factory=list)

    # --- vector ------------------------------------------------------------ #
    #: Kept out of API responses (see `public_dump`) but persisted so the vector
    #: index is disposable and rebuildable.
    embedding: list[float] | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = None

    # --- bookkeeping -------------------------------------------------------- #
    content_hash: str = ""

    # --- recall ------------------------------------------------------------ #
    #
    # How often this memory has actually been *useful*, as opposed to how often
    # it has been written to. It is the signal consolidation needs in order to
    # forget anything responsibly: age alone is the wrong measure, because a
    # peanut allergy nobody has asked about in a year still matters more than
    # yesterday's lunch order.
    #
    # Deliberately kept out of `updated_at`. A read is not a write, and letting
    # searches bump `updated_at` would feed the recency term of the ranker back
    # into itself — every search would make its own results look fresher, and
    # popular memories would climb forever.
    last_recalled_at: datetime | None = None
    recall_count: int = 0

    #: Set when a source memory of a pattern changes state, marking the pattern
    #: for re-evaluation. Never triggers automatic deletion (see §32).
    stale: bool = False
    last_synthesized_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_bot_id(cls, data):
        """`bot_id` was the previous name for `agent_id`."""
        if isinstance(data, dict) and ("bot_id" in data or "botId" in data):
            data = dict(data)
            legacy = data.pop("bot_id", None) or data.pop("botId", None)
            if legacy is not None:
                data.setdefault("agent_id", legacy)
        return data

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("memory content must not be blank")
        return value.strip()

    @field_validator("memory_type")
    @classmethod
    def _normalise_type(cls, value: str) -> str:
        # Open vocabulary, but normalised so "Preference" and "preference" are one type.
        return (value or MemoryType.OTHER.value).strip().lower() or MemoryType.OTHER.value

    def model_post_init(self, _context: Any) -> None:
        if not self.content_hash:
            object.__setattr__(self, "content_hash", content_fingerprint(self.content))
        if not self.scope_key:
            # Derived here rather than at each construction site, so a memory can
            # never be written without the key its own partition is filtered on.
            object.__setattr__(self, "scope_key", self.scope.scope_key)

    @property
    def scope(self) -> MemoryScope:
        """The partition this memory belongs to."""
        from app.models.scope import MemoryScope

        return MemoryScope(
            user_id=self.user_id,
            agent_id=self.agent_id,
            client_id=self.client_id,
            namespace=self.namespace,
        )

    # --- helpers ----------------------------------------------------------- #

    @property
    def is_pattern(self) -> bool:
        return self.memory_type == MemoryType.PATTERN.value

    @property
    def is_active(self) -> bool:
        return self.status == MemoryStatus.ACTIVE

    def touch(self) -> None:
        self.updated_at = utcnow()


    @property
    def bot_id(self) -> str | None:
        """Previous name for `agent_id`. Kept so existing callers keep working."""
        return self.agent_id

    def to_document(self) -> dict[str, Any]:
        """Serialise for Mongo, using ``memoryId`` as the primary key."""
        doc = self.model_dump(by_alias=True, exclude_none=False)
        doc["_id"] = doc.pop("memoryId")
        doc["status"] = self.status.value
        doc["temporalNature"] = self.temporal_nature.value
        return doc

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> Memory:
        data = dict(document)
        if "_id" in data:
            data["memoryId"] = data.pop("_id")
        return cls.model_validate(data)

    def public_dump(self, include_embedding: bool = False) -> dict[str, Any]:
        """API-facing representation. Drops the embedding by default — it is
        large, uninteresting to callers, and re-derivable."""
        data = self.model_dump(by_alias=True, mode="json")
        if not include_embedding:
            data.pop("embedding", None)
        return data


# scope-keyed:MemoryRelation
class MemoryRelation(BaseModel):
    """A directed edge between two memories in the same scope.

    Edges are written in both directions (``supersedes`` and ``superseded_by``)
    so either endpoint can be queried without a second index.
    """

    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True)

    relation_id: str = Field(default_factory=lambda: f"rel_{uuid.uuid4().hex[:24]}")
    user_id: str
    agent_id: str | None = None
    client_id: str | None = None
    namespace: dict[str, str] = Field(default_factory=dict)
    scope_key: str = ""

    from_memory_id: str
    to_memory_id: str
    relation_type: RelationType

    created_at: datetime = Field(default_factory=utcnow)
    #: The Dream operation that created this edge, for audit joins.
    operation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


    @model_validator(mode="before")
    @classmethod
    def _scope_compat(cls, data):
        """Accept the old `bot_id` spelling and derive the partition key.

        Derived here rather than at each construction site, so a row can never
        be written without the key its own partition is filtered on.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)

        legacy = data.pop("bot_id", None) or data.pop("botId", None)
        if legacy is not None:
            data.setdefault("agent_id", legacy)

        if not data.get("scope_key") and not data.get("scopeKey"):
            from app.models.scope import MemoryScope

            user_id = data.get("user_id") or data.get("userId")
            if user_id:
                data["scope_key"] = MemoryScope(
                    user_id=user_id,
                    agent_id=data.get("agent_id") or data.get("agentId"),
                    client_id=data.get("client_id") or data.get("clientId"),
                    namespace=data.get("namespace") or {},
                ).scope_key
        return data


    @property
    def bot_id(self) -> str | None:
        """Previous name for `agent_id`. Kept so existing callers keep working."""
        return self.agent_id

    def to_document(self) -> dict[str, Any]:
        doc = self.model_dump(by_alias=True)
        doc["_id"] = doc.pop("relationId")
        doc["relationType"] = self.relation_type.value
        return doc

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> MemoryRelation:
        data = dict(document)
        if "_id" in data:
            data["relationId"] = data.pop("_id")
        return cls.model_validate(data)


class ScoredMemory(BaseModel):
    """A memory plus the retrieval scores that selected it.

    The component scores travel with the result so that ranking decisions are
    explainable in logs and in the API response, not just the final number.
    """

    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True)

    memory: Memory
    score: float
    semantic_similarity: float
    importance_score: float = 0.0
    confidence_score: float = 0.0
    recency_score: float = 0.0
    status_score: float = 0.0

    def public_dump(self) -> dict[str, Any]:
        data = self.memory.public_dump()
        data["score"] = round(self.score, 6)
        data["scoreBreakdown"] = {
            "semanticSimilarity": round(self.semantic_similarity, 6),
            "importance": round(self.importance_score, 6),
            "confidence": round(self.confidence_score, 6),
            "recency": round(self.recency_score, 6),
            "status": round(self.status_score, 6),
        }
        return data
