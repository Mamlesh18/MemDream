"""Validation of model proposals, and construction of the memories they imply.

This module is the enforcement half of "LLM-assisted, not LLM-controlled". A
`DreamDecision` arrives as an untrusted suggestion; nothing in it reaches the
database until it has passed through here.

Specifically:

* Target ids are intersected with the ids that were actually shown to the model.
  A hallucinated or copied-from-elsewhere id is dropped, not looked up — which
  is what makes it impossible for a model to reach another tenant's memory by
  guessing an id.
* Scores are clamped to their valid range.
* Tenancy is taken from the request's `MemoryScope` and never from model output.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.models.dream import DreamDecision, ExtractedMemory, ProposedMemory
from app.models.enums import DreamAction, MemoryStatus
from app.models.memory import Memory, utcnow
from app.models.scope import MemoryScope

logger = get_logger(__name__)


@dataclass(slots=True)
class ValidatedDecision:
    """A decision that is safe to execute."""

    action: DreamAction
    reason: str
    proposal: ProposedMemory | None
    #: Target ids, already intersected with what the model was shown.
    target_ids: list[str]
    #: Human-readable record of every correction made to the raw proposal.
    notes: list[str] = field(default_factory=list)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def validate_decision(
    decision: DreamDecision,
    *,
    supplied_ids: list[str],
    supersede_enabled: bool = True,
    merge_enabled: bool = True,
) -> ValidatedDecision:
    """Reduce a raw decision to something that can be executed safely."""
    notes: list[str] = []
    supplied = set(supplied_ids)

    # Only ids the model was actually given may be acted on.
    targets = [mid for mid in dict.fromkeys(decision.target_memory_ids) if mid in supplied]
    invented = [mid for mid in decision.target_memory_ids if mid not in supplied]
    if invented:
        notes.append(f"dropped {len(invented)} target id(s) not supplied to the model: {invented}")
        logger.warning("dream_decision_invented_ids", ids=invented)

    action = decision.action
    proposal = decision.memory

    # A disabled operation degrades rather than failing: the fact is still worth
    # keeping, it just gets stored alongside instead of consolidated.
    if action == DreamAction.SUPERSEDE and not supersede_enabled:
        notes.append("supersede is disabled by configuration; storing as ADD")
        action, targets = DreamAction.ADD, []
    if action == DreamAction.MERGE and not merge_enabled:
        notes.append("merge is disabled by configuration; storing as ADD")
        action, targets = DreamAction.ADD, []

    # SUPERSEDE and MERGE are meaningless without something to act on.
    if action in (DreamAction.SUPERSEDE, DreamAction.MERGE) and not targets:
        notes.append(f"{action.value} had no valid target memories; downgraded to ADD")
        action = DreamAction.ADD

    # ...and without a memory to put in their place.
    if action in (DreamAction.ADD, DreamAction.SUPERSEDE, DreamAction.MERGE) and (
        proposal is None or not proposal.content.strip()
    ):
        notes.append(f"{action.value} returned no memory content; downgraded to NOOP")
        action, targets = DreamAction.NOOP, targets if action != DreamAction.ADD else []
        proposal = None

    if action == DreamAction.NOOP:
        proposal = None

    if proposal is not None:
        proposal.confidence = _clamp(proposal.confidence)
        proposal.importance = _clamp(proposal.importance)

    return ValidatedDecision(
        action=action,
        reason=decision.reason.strip(),
        proposal=proposal,
        target_ids=targets,
        notes=notes,
    )


def build_memory(
    scope: MemoryScope,
    *,
    proposal: ProposedMemory | ExtractedMemory,
    embedding: list[float] | None = None,
    embedding_model: str | None = None,
    message_ids: list[str] | None = None,
    source_memory_ids: list[str] | None = None,
    metadata: dict | None = None,
) -> Memory:
    """Construct a `Memory` from a proposal.

    Tenancy comes from `scope` — the caller's authenticated context — and is not
    readable from, or overridable by, anything the model produced.
    """
    now = utcnow()
    explicit = getattr(proposal, "explicit", True)

    return Memory(
        **scope.as_model_fields(),
        phone_number_hash=scope.phone_number_hash,
        content=proposal.content.strip(),
        memory_type=proposal.memory_type,
        status=MemoryStatus.ACTIVE,
        importance=_clamp(proposal.importance),
        confidence=_clamp(proposal.confidence),
        explicit=bool(explicit),
        temporal_nature=proposal.temporal_nature,
        # A fact with no stated start is treated as true from when we learned it,
        # which keeps point-in-time queries answerable without inventing history.
        valid_from=proposal.valid_from or now,
        valid_until=proposal.valid_until,
        created_at=now,
        updated_at=now,
        embedding=embedding,
        embedding_model=embedding_model,
        embedding_dim=len(embedding) if embedding else None,
        source_message_ids=list(message_ids or []),
        source_memory_ids=list(source_memory_ids or []),
        metadata=dict(metadata or {}),
    )
