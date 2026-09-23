"""Turning a user message into candidate memories.

This is the one place the LLM is asked an open-ended question. Everything it
returns is then narrowed by deterministic code: the policy decides what may be
stored, and the Dream engine decides where it goes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.config import Settings, get_settings
from app.core.exceptions import LLMResponseError, ProviderError
from app.core.logging import get_logger
from app.core.metrics import EXTRACTION_SKIPPED, OP_EXTRACT, metrics, operation
from app.core.policy import MemoryPolicy
from app.core.prompts import PromptLibrary
from app.dream.prompts import build_extraction_payload
from app.memory.triage import skip_reason
from app.models.dream import ExtractedMemory, ExtractionResult
from app.models.memory import utcnow
from app.providers.llm.base import LLMProvider

logger = get_logger(__name__)


@dataclass(slots=True)
class ExtractionOutcome:
    """Everything the caller needs, including why nothing was extracted."""

    candidates: list[ExtractedMemory] = field(default_factory=list)
    rejected: list[tuple[ExtractedMemory, str]] = field(default_factory=list)
    reason: str = ""
    model: str | None = None
    latency_ms: float = 0.0
    token_usage: dict[str, int] = field(default_factory=dict)
    #: True when the gate answered without an LLM call at all. Keeps
    #: "nothing worth remembering" distinguishable from "we never asked".
    skipped: bool = False

    @property
    def has_memories(self) -> bool:
        return bool(self.candidates)


class MemoryExtractor:
    def __init__(
        self,
        llm: LLMProvider,
        *,
        policy: MemoryPolicy | None = None,
        settings: Settings | None = None,
        prompts: PromptLibrary | None = None,
    ) -> None:
        self._llm = llm
        self._settings = settings or get_settings()
        self._policy = policy or MemoryPolicy(self._settings.memory)
        self._prompts = prompts or PromptLibrary.from_settings(self._settings, validate=False)

    @property
    def policy(self) -> MemoryPolicy:
        return self._policy

    async def extract(
        self,
        prompt: str,
        *,
        now: datetime | None = None,
        message_id: str | None = None,
    ) -> ExtractionOutcome:
        """Extract durable memories from one message.

        An extraction failure is not allowed to fail the caller's request: a
        message that could not be mined is a message with no memories, which is
        already the common case. The error is logged and surfaced in `reason`.
        """
        text = (prompt or "").strip()

        # The cheapest decision in the system. A greeting costs one token to say
        # and ~660 to reason about, and cannot contain a memory. The gate only
        # fires when it is certain — see app/memory/triage.py for why it is a
        # lexicon rather than a heuristic.
        if self._settings.triage_enabled and (reason := skip_reason(text)):
            metrics.increment(EXTRACTION_SKIPPED, labels={"reason": reason})
            logger.debug("extraction_skipped", reason=reason)
            return ExtractionOutcome(reason=reason, skipped=True)

        try:
            with operation(OP_EXTRACT):
                result = await self._llm.generate_json(
                    system=self._prompts.get("extraction"),
                    user=build_extraction_payload(
                        text, now=now or utcnow(), message_id=message_id
                    ),
                    response_model=ExtractionResult,
                )
        except (LLMResponseError, ProviderError) as exc:
            logger.error("extraction_failed", error=str(exc)[:300])
            return ExtractionOutcome(reason=f"extraction failed: {exc}")

        extraction = result.data
        if not extraction.has_memory or not extraction.memories:
            return ExtractionOutcome(
                reason=extraction.reason or "no durable memory in this message",
                model=result.model,
                latency_ms=result.latency_ms,
                token_usage=result.usage,
            )

        # Drop blanks the model occasionally emits when it decides mid-response
        # that an item is not worth keeping.
        candidates = [item for item in extraction.memories if item.content.strip()]
        accepted, rejected = self._policy.filter(candidates)

        logger.info(
            "memories_extracted",
            proposed=len(candidates),
            accepted=len(accepted),
            rejected=len(rejected),
            model=result.model,
        )
        return ExtractionOutcome(
            candidates=accepted,
            rejected=rejected,
            reason=extraction.reason,
            model=result.model,
            latency_ms=result.latency_ms,
            token_usage=result.usage,
        )

    def as_raw_candidate(self, prompt: str) -> ExtractedMemory:
        """Wrap a prompt as a single memory with no LLM involvement.

        Used by ``raw: true`` callers that have already decided what to remember
        and want the Dream layer's deduplication but not its extraction.
        """
        return ExtractedMemory(
            content=prompt.strip(),
            memory_type="other",
            importance=0.5,
            confidence=1.0,
            explicit=True,
        )
