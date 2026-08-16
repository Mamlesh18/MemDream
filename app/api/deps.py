"""FastAPI dependencies: container access, authentication, scope resolution.

The scope helpers here are the only place a `MemoryScope` is built from request
input. Everything downstream receives an already-resolved scope, so no route
handler is in a position to forget the tenancy check.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, Query, Request

from app.container import DreamContainer
from app.core.config import Settings, get_settings
from app.core.exceptions import UnauthorizedError
from app.core.identity import build_scope
from app.core.logging import bind_scope
from app.models.requests import _ScopedRequest
from app.models.scope import MemoryScope


def get_container(request: Request) -> DreamContainer:
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - only reachable if startup failed
        raise RuntimeError("Dream container is not initialised")
    return container


ContainerDep = Annotated[DreamContainer, Depends(get_container)]


async def require_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """Validate the API key when any are configured.

    An empty ``DREAM_API_KEYS`` disables authentication — appropriate when the
    service runs inside a private network behind its own gateway, and the reason
    it is opt-in rather than mandatory.
    """
    settings: Settings = get_container(request).settings
    if not settings.auth_enabled:
        return

    if not x_api_key:
        raise UnauthorizedError("Missing X-API-Key header")

    # Constant-time comparison against each configured key: a timing side channel
    # on an auth check is cheap to avoid.
    if not any(hmac.compare_digest(x_api_key, key) for key in settings.api_keys):
        raise UnauthorizedError("Invalid API key")


AuthDep = Annotated[None, Depends(require_api_key)]


def scope_from_request(body: _ScopedRequest, settings: Settings | None = None) -> MemoryScope:
    """Resolve the scope for a request body, and bind it to the log context."""
    scope = build_scope(
        client_id=body.client_id,
        bot_id=body.bot_id,
        phone_number=body.phone_number,
        user_id=body.user_id,
        settings=settings or get_settings(),
    )
    bind_scope(scope.client_id, scope.bot_id, scope.user_id)
    return scope


async def scope_from_query(
    client_id: Annotated[str, Query(alias="clientId", min_length=1)],
    bot_id: Annotated[str, Query(alias="botId", min_length=1)],
    phone_number: Annotated[str | None, Query(alias="phoneNumber")] = None,
    user_id: Annotated[str | None, Query(alias="userId")] = None,
) -> MemoryScope:
    """Scope for GET endpoints.

    Every read is scoped, including reads by id. A memory id is a random handle,
    not an authorisation token, so possessing one must not be enough to read the
    record.
    """
    scope = build_scope(
        client_id=client_id,
        bot_id=bot_id,
        phone_number=phone_number,
        user_id=user_id,
    )
    bind_scope(scope.client_id, scope.bot_id, scope.user_id)
    return scope


ScopeDep = Annotated[MemoryScope, Depends(scope_from_query)]
