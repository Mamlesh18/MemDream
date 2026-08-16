"""Memory policy — the guard rail between extraction and persistence.

Everything the extractor proposes passes through here before it can become a
memory. The checks are deliberately deterministic: whether a deployment is
allowed to store health data is a policy question with a configured answer, not
something to ask a language model about.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import MemoryPolicySettings
from app.core.logging import get_logger
from app.models.dream import ExtractedMemory

logger = get_logger(__name__)


@dataclass(slots=True)
class PolicyDecision:
    accepted: bool
    reason: str = ""

    @classmethod
    def accept(cls) -> PolicyDecision:
        return cls(accepted=True)

    @classmethod
    def reject(cls, reason: str) -> PolicyDecision:
        return cls(accepted=False, reason=reason)


class MemoryPolicy:
    """Applies the configured limits to candidate memories."""

    def __init__(self, settings: MemoryPolicySettings | None = None) -> None:
        self._settings = settings or MemoryPolicySettings()
        self._allowed = {t.strip().lower() for t in self._settings.allowed_memory_types if t.strip()}
        self._blocked = {t.strip().lower() for t in self._settings.blocked_memory_types if t.strip()}

    @property
    def settings(self) -> MemoryPolicySettings:
        return self._settings

    def evaluate(self, candidate: ExtractedMemory) -> PolicyDecision:
        content = (candidate.content or "").strip()
        if not content:
            return PolicyDecision.reject("empty content")

        if len(content) > self._settings.max_content_chars:
            return PolicyDecision.reject(
                f"content exceeds {self._settings.max_content_chars} characters"
            )

        memory_type = candidate.memory_type.strip().lower()
        if memory_type in self._blocked:
            return PolicyDecision.reject(f"memory type '{memory_type}' is blocked by policy")
        # An empty allow-list means "no restriction", not "nothing allowed".
        if self._allowed and memory_type not in self._allowed:
            return PolicyDecision.reject(f"memory type '{memory_type}' is not in the allow-list")

        if candidate.sensitive and not self._settings.allow_sensitive:
            return PolicyDecision.reject("candidate was flagged sensitive and policy forbids it")

        if candidate.confidence < self._settings.min_confidence:
            return PolicyDecision.reject(
                f"confidence {candidate.confidence:.2f} is below the minimum "
                f"{self._settings.min_confidence:.2f}"
            )

        if candidate.importance < self._settings.min_importance:
            return PolicyDecision.reject(
                f"importance {candidate.importance:.2f} is below the minimum "
                f"{self._settings.min_importance:.2f}"
            )

        return PolicyDecision.accept()

    def filter(
        self, candidates: list[ExtractedMemory]
    ) -> tuple[list[ExtractedMemory], list[tuple[ExtractedMemory, str]]]:
        """Split candidates into accepted and (candidate, reason) rejections.

        Rejections are returned rather than dropped so the caller can record them
        in the audit log — "why was nothing remembered?" needs an answer.
        """
        accepted: list[ExtractedMemory] = []
        rejected: list[tuple[ExtractedMemory, str]] = []

        for candidate in candidates:
            decision = self.evaluate(candidate)
            if decision.accepted:
                accepted.append(candidate)
            else:
                rejected.append((candidate, decision.reason))

        limit = self._settings.max_candidates_per_prompt
        if len(accepted) > limit:
            # Keep the most important ones; a single message producing dozens of
            # memories is usually an extraction failure, not a rich message.
            accepted.sort(key=lambda c: c.importance, reverse=True)
            for candidate in accepted[limit:]:
                rejected.append((candidate, f"exceeded the per-prompt limit of {limit}"))
            accepted = accepted[:limit]

        if rejected:
            logger.info(
                "policy_rejected_candidates",
                accepted=len(accepted),
                rejected=len(rejected),
                reasons=[reason for _, reason in rejected[:5]],
            )
        return accepted, rejected
