"""The merge engine.

Merge answers: is this the same fact I already know, possibly said better?

Unlike supersede, merge has a cheap deterministic fast path. An exact duplicate —
same text once normalised for case, whitespace and trailing punctuation — needs
no model call at all, and in chat-shaped traffic that case is common enough to
be worth catching before spending a token.

The canonical memory must never lose information. That is checked in code as
well as asked for in the prompt: a proposed canonical text that is shorter than
what it claims to replace, and does not contain it, is rejected.
"""

from __future__ import annotations

from datetime import datetime

from app.core.config import MergeSettings
from app.core.logging import get_logger
from app.dream.prompts import MERGE_SYSTEM, build_decision_payload
from app.dream.validation import ValidatedDecision, validate_decision
from app.memory.lifecycle import MemoryLifecycle
from app.models.dream import DreamDecision
from app.models.enums import DreamAction, MemoryStatus, MemoryType
from app.models.memory import Memory, normalize_content
from app.models.scope import MemoryScope
from app.providers.llm.base import LLMProvider, LLMResult
from app.repositories.memory_repository import MemoryRepository

logger = get_logger(__name__)


class MergeEngine:
    def __init__(
        self,
        llm: LLMProvider,
        lifecycle: MemoryLifecycle,
        repository: MemoryRepository,
        settings: MergeSettings | None = None,
    ) -> None:
        self._llm = llm
        self._lifecycle = lifecycle
        self._repository = repository
        self._settings = settings or MergeSettings()

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    # --- deterministic fast path -------------------------------------------------- #

    async def find_exact_duplicate(self, scope: MemoryScope, content: str) -> Memory | None:
        """An active memory with identical normalised content, if one exists.

        Indexed lookup on `contentHash` — no embedding, no LLM call.
        """
        if not self._settings.exact_duplicate_shortcut:
            return None
        from app.models.memory import content_fingerprint

        return await self._repository.find_by_content_hash(
            scope, content_fingerprint(content), statuses=(MemoryStatus.ACTIVE,)
        )

    # --- candidate filtering ------------------------------------------------------- #

    def eligible_targets(
        self, candidate_type: str, related: list[tuple[Memory, float]]
    ) -> list[tuple[Memory, float]]:
        """Memories close enough to be the same fact.

        A higher bar than supersede's, and additionally restricted to the same
        memory type: a `preference` and a `constraint` about the same subject are
        two different things about it, not one thing said twice.
        """
        eligible: list[tuple[Memory, float]] = []
        for memory, similarity in related:
            if similarity < self._settings.similarity_threshold:
                continue
            if memory.memory_type == MemoryType.PATTERN.value:
                continue
            if candidate_type and memory.memory_type != candidate_type:
                continue
            eligible.append((memory, similarity))

        eligible.sort(key=lambda pair: pair[1], reverse=True)
        return eligible[: self._settings.max_candidates]

    # --- reasoning ------------------------------------------------------------------ #

    async def evaluate(
        self,
        candidate: dict,
        related: list[tuple[Memory, float]],
        *,
        now: datetime | None = None,
    ) -> tuple[ValidatedDecision, LLMResult[DreamDecision]]:
        """Ask the focused merge question. Used in ``staged`` decision mode."""
        result = await self._llm.generate_json(
            system=MERGE_SYSTEM,
            user=build_decision_payload(candidate, related, now=now, task="decide"),
            response_model=DreamDecision,
        )

        decision = result.data
        if decision.action not in (DreamAction.MERGE, DreamAction.NOOP):
            logger.info(
                "merge_stage_returned_other_action",
                action=decision.action.value,
                detail="coerced to NOOP; the supersede stage handles contradictions",
            )
            decision = DreamDecision(
                action=DreamAction.NOOP,
                reason=decision.reason,
                memory=None,
                target_memory_ids=[],
            )

        validated = validate_decision(
            decision,
            supplied_ids=[memory.memory_id for memory, _ in related],
            merge_enabled=self._settings.enabled,
        )
        if validated.action == DreamAction.ADD:
            validated.action = DreamAction.NOOP
            validated.proposal = None

        return validated, result

    # --- information-preservation check ----------------------------------------------- #

    def check_no_information_lost(
        self, canonical_content: str, targets: list[Memory], candidate_content: str
    ) -> list[str]:
        """Warn when a canonical text looks like it dropped something.

        A heuristic, not a proof: it flags a canonical memory materially shorter
        than an input it subsumes. Callers use the notes for the audit log rather
        than to block the merge, because legitimate merges do compress ("User
        likes coffee. User likes black coffee." -> "User likes black coffee.").
        """
        notes: list[str] = []
        canonical_normalized = normalize_content(canonical_content)

        for source in [*(memory.content for memory in targets), candidate_content]:
            source_normalized = normalize_content(source)
            if not source_normalized or source_normalized in canonical_normalized:
                continue
            if len(canonical_normalized) < len(source_normalized) * 0.75:
                notes.append(
                    f"canonical text is materially shorter than an input it replaces: "
                    f"{source[:80]!r}"
                )
        return notes

    # --- execution ----------------------------------------------------------------- #

    async def apply(
        self,
        scope: MemoryScope,
        *,
        canonical: Memory,
        targets: list[Memory],
        operation_id: str | None = None,
    ) -> list[str]:
        """Fold ``targets`` into ``canonical``."""
        if not targets:
            return []
        return await self._lifecycle.merge(
            scope, canonical=canonical, targets=targets, operation_id=operation_id
        )
