"""SDK exceptions.

The API returns a stable ``error.code``; these map it to a type so callers can
branch on failures without string matching.
"""

from __future__ import annotations

from typing import Any


class DreamError(Exception):
    """Base for every SDK error."""


class DreamConnectionError(DreamError):
    """The service could not be reached."""


class DreamAPIError(DreamError):
    """The service returned an error response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        code: str = "",
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.details = details or {}
        self.request_id = request_id

    def __str__(self) -> str:
        parts = [self.message]
        if self.code:
            parts.append(f"code={self.code}")
        if self.status_code:
            parts.append(f"status={self.status_code}")
        if self.request_id:
            # Included so a user report can be traced to a server-side log line.
            parts.append(f"requestId={self.request_id}")
        return " (".join([parts[0], ", ".join(parts[1:]) + ")"]) if len(parts) > 1 else parts[0]


class DreamAuthError(DreamAPIError):
    """The API key was missing or rejected."""


class DreamNotFoundError(DreamAPIError):
    """The memory or job does not exist, or is outside the requesting scope."""


class DreamValidationError(DreamAPIError):
    """The request body failed validation."""


def error_for(status_code: int, payload: dict[str, Any], request_id: str | None) -> DreamAPIError:
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    message = error.get("message") or f"HTTP {status_code}"
    code = error.get("code", "")
    details = error.get("details", {})

    kwargs = {
        "status_code": status_code,
        "code": code,
        "details": details,
        "request_id": request_id,
    }
    if status_code == 401:
        return DreamAuthError(message, **kwargs)
    if status_code == 404:
        return DreamNotFoundError(message, **kwargs)
    if status_code == 422:
        return DreamValidationError(message, **kwargs)
    return DreamAPIError(message, **kwargs)
