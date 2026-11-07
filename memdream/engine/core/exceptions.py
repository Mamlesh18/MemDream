"""Domain exceptions.

Each carries an HTTP status so the API layer can translate without a big
isinstance ladder, and a stable ``code`` so clients can branch on failures
without string-matching messages.
"""

from __future__ import annotations

from typing import Any


class DreamError(Exception):
    """Base class for every error this framework raises deliberately."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            payload["error"]["details"] = self.details
        return payload


class ConfigurationError(DreamError):
    status_code = 500
    code = "configuration_error"


class ValidationError(DreamError):
    status_code = 422
    code = "validation_error"


class NotFoundError(DreamError):
    status_code = 404
    code = "not_found"


class UnauthorizedError(DreamError):
    status_code = 401
    code = "unauthorized"


class TenantIsolationError(DreamError):
    """Raised when an operation would cross a client/bot/user boundary.

    This should never surface in normal operation — it means a scope check was
    missed upstream, so it is deliberately loud rather than a silent empty result.
    """

    status_code = 403
    code = "tenant_isolation_violation"


class ProviderError(DreamError):
    """An upstream provider (LLM, embeddings, vector store) failed."""

    status_code = 502
    code = "provider_error"


class LLMResponseError(ProviderError):
    """The LLM returned something that did not satisfy the requested schema."""

    code = "llm_response_error"


class InvalidStateTransitionError(DreamError):
    """A memory lifecycle transition was rejected (e.g. superseding a merged row)."""

    status_code = 409
    code = "invalid_state_transition"


class JobNotFoundError(NotFoundError):
    code = "job_not_found"
