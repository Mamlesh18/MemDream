"""The supersede engine.

Supersede answers one question: does this new fact make an existing one untrue?

It is the operation with the most to lose from a false positive. Wrongly merging
two memories costs a little nuance; wrongly superseding one removes the current
answer to a question and replaces it with something that is not true. So the
bias throughout — in the prompt, and in the filters below — is towards leaving
memories alone.
"""

from __future__ import annotations

from datetime import datetime

from app.core.config import SupersedeSettings
from app.core.logging import get_logger
from app.dream.prompts import SUPERSEDE_SYSTEM, build_decision_payload
from app.dream.validation import ValidatedDecision, validate_decision
from app.memory.lifecycle import MemoryLifecycle
from app.models.dream import DreamDecision
from app.models.enums import DreamAction, MemoryType, TemporalNature
from app.models.memory import Memory
from app.models.scope import MemoryScope
from app.providers.llm.base import LLMProvider, LLMResult

logger = get_logger(__name__)


class SupersedeEngine:
    def __init__(
        self,
        llm: LLMProvider,
        lifecycle: MemoryLifecycle,
        settings: SupersedeSettings | None = None,
    ) -> None:
        self._llm = llm
        self._lifecycle = lifecycle
        self._settings = settings or SupersedeSettings()

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    # --- candidate filtering ---------------------------------------------------- #

    def eligible_targets(
        self, candidate_type: str, related: list[tuple[Memory, float]]
    ) -> list[tuple[Memory, float]]:
        """Narrow the related set to memories that could plausibly be replaced.

        Three deterministic exclusions, applied before any token is spent:

        * **Below threshold.** Unrelated memories cannot be contradicted.
        * **Patterns.** A synthesised generalisation is derived knowledge; it is
          re-evaluated by synthesis, never retired by an incoming raw fact.
        * **Permanent facts.** Where someone was born does not stop being true
          when they move. Encoding this in code rather than trusting the prompt
          means it holds even when a model has an off day.
        """
        eligible: list[tuple[Memory, float]] = []
        for memory, similarity in related:
            if similarity < self._settings.similarity_threshold:
                continue
            if memory.memory_type == MemoryType.PATTERN.value:
                continue
            if memory.temporal_nature == TemporalNature.PERMANENT:
                continue
            eligible.append((memory, similarity))

        eligible.sort(key=lambda pair: pair[1], reverse=True)
        return eligible[: self._settings.max_candidates]

    # --- reasoning ---------------------------------------------------------------- #

    async def evaluate(
        self,
        candidate: dict,
        related: list[tuple[Memory, float]],
        *,
        now: datetime | None = None,
    ) -> tuple[ValidatedDecision, LLMResult[DreamDecision]]:
        """Ask the focused supersede question. Used in ``staged`` decision mode."""
        result = await self._llm.generate_json(
            system=SUPERSEDE_SYSTEM,
            user=build_decision_payload(candidate, related, now=now, task="decide"),
            response_model=DreamDecision,
        )

        decision = result.data
        # This prompt has only two legal outcomes. Anything else is the model
        # answering a question it was not asked, and is treated as "no change".
        if decision.action not in (DreamAction.SUPERSEDE, DreamAction.NOOP):
            logger.info(
                "supersede_stage_returned_other_action",
                action=decision.action.value,
                detail="coerced to NOOP; the merge stage handles consolidation",
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
            supersede_enabled=self._settings.enabled,
        )
        # `validate_decision` downgrades a target-less SUPERSEDE to ADD, which is
        # the right default for the unified path but wrong here: this stage is
        # only deciding whether to retire something.
        if validated.action == DreamAction.ADD:
            validated.action = DreamAction.NOOP
            validated.proposal = None

        return validated, result

    # --- execution -------------------------------------------------------------- #

    async def apply(
        self,
        scope: MemoryScope,
        *,
        new_memory: Memory,
        targets: list[Memory],
        valid_until: datetime | None = None,
        operation_id: str | None = None,
    ) -> list[str]:
        """Retire ``targets`` in favour of ``new_memory``.

        Returns the ids that actually transitioned — which may be fewer than
        requested if a target had already reached a terminal state.
        """
        if not targets:
            return []
        return await self._lifecycle.supersede(
            scope,
            new_memory=new_memory,
            targets=targets,
            valid_until=valid_until,
            operation_id=operation_id,
        )
