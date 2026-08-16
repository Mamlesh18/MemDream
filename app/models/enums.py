"""Enumerations.

`MemoryType` is intentionally *not* a closed enum. Deployments invent their own
types ("dietary_restriction", "billing_tier"), and the engine must not care:
nothing in this codebase branches on a memory type except the pattern type,
which synthesis owns. The constants below are the well-known set used in
prompts and validation hints only.
"""

from __future__ import annotations

from enum import Enum


class MemoryStatus(str, Enum):
    """Lifecycle state. Dream operations never delete; they transition."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    MERGED = "merged"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        """Terminal rows are historical: readable, never mutated again."""
        return self in (MemoryStatus.SUPERSEDED, MemoryStatus.MERGED)


#: States a memory may be retrieved from by default (active + history).
DEFAULT_SEARCH_STATUSES: tuple[MemoryStatus, ...] = (
    MemoryStatus.ACTIVE,
    MemoryStatus.SUPERSEDED,
)


class MemoryType(str, Enum):
    """Well-known memory types. Arbitrary strings are also accepted."""

    PROFILE = "profile"
    PREFERENCE = "preference"
    GOAL = "goal"
    FACT = "fact"
    EVENT = "event"
    RELATIONSHIP = "relationship"
    CONSTRAINT = "constraint"
    SKILL = "skill"
    PROJECT = "project"
    BEHAVIOR = "behavior"
    PATTERN = "pattern"
    OTHER = "other"


#: The one type with engine semantics attached: produced by synthesis, and
#: excluded from the evidence pool so patterns never feed on patterns.
PATTERN_TYPE = MemoryType.PATTERN.value

KNOWN_MEMORY_TYPES: frozenset[str] = frozenset(t.value for t in MemoryType)


#: Types that can never genuinely be permanent, whatever the extractor labelled
#: them. A person can always change their mind, their habits, their goals or
#: their job. Used by the supersede guard so that one mislabel at extraction time
#: cannot freeze a mutable memory beyond correction.
INHERENTLY_MUTABLE_TYPES: frozenset[str] = frozenset(
    {
        MemoryType.PREFERENCE.value,
        MemoryType.BEHAVIOR.value,
        MemoryType.GOAL.value,
        MemoryType.PROJECT.value,
        MemoryType.CONSTRAINT.value,
        MemoryType.SKILL.value,
    }
)


class DreamAction(str, Enum):
    """What the real-time Dream engine decided to do with a candidate memory."""

    ADD = "ADD"
    SUPERSEDE = "SUPERSEDE"
    MERGE = "MERGE"
    NOOP = "NOOP"


class SynthesisAction(str, Enum):
    """What the synthesis reasoner decided about a cluster of memories."""

    SYNTHESIZE = "SYNTHESIZE"
    NOOP = "NOOP"


class RelationType(str, Enum):
    """Edges between memories.

    Stored as explicit rows rather than only as pointer fields, because the
    pointer fields answer "what replaced this?" while the edge table answers
    "what does this pattern rest on?" — a many-to-many question.
    """

    SUPERSEDES = "supersedes"
    SUPERSEDED_BY = "superseded_by"
    MERGED_INTO = "merged_into"
    MERGED_FROM = "merged_from"
    DERIVED_FROM = "derived_from"
    SUPPORTS = "supports"

    @property
    def inverse(self) -> RelationType:
        return _INVERSE_RELATIONS[self]


_INVERSE_RELATIONS: dict[RelationType, RelationType] = {
    RelationType.SUPERSEDES: RelationType.SUPERSEDED_BY,
    RelationType.SUPERSEDED_BY: RelationType.SUPERSEDES,
    RelationType.MERGED_INTO: RelationType.MERGED_FROM,
    RelationType.MERGED_FROM: RelationType.MERGED_INTO,
    RelationType.DERIVED_FROM: RelationType.SUPPORTS,
    RelationType.SUPPORTS: RelationType.DERIVED_FROM,
}


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TemporalNature(str, Enum):
    """How a fact relates to time — drives validFrom/validUntil handling.

    A `PERMANENT` fact ("born in Chennai") is never superseded by a later
    location; a `CURRENT` one ("lives in Chennai") is.
    """

    PERMANENT = "permanent"
    CURRENT = "current"
    TEMPORARY = "temporary"
    HISTORICAL = "historical"
    FUTURE = "future"
