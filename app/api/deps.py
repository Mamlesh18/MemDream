"""FastAPI dependencies: container access, authentication, scope resolution.

The scope helpers here are the only place a `MemoryScope` is built from request
input. Everything downstream receives an already-resolved scope, so no route
handler is in a position to forget the tenancy check.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import Depends, Header, Query, Request

from app.container import DreamContainer
from app.core.auth import Principal
from app.core.config import Settings, get_settings
from app.core.exceptions import ValidationError
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


async def resolve_principal(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Principal:
    """Authenticate the request and determine which tenant it acts as.

    Returns an anonymous principal when no keys are configured — a self-hosted
    deployment behind its own gateway has nobody to impersonate.
    """
    registry = get_container(request).api_keys
    return await registry.resolve(x_api_key)


PrincipalDep = Annotated[Principal, Depends(resolve_principal)]
#: Retained so existing routes keep working; prefer `PrincipalDep`.
AuthDep = PrincipalDep


def scope_from_request(
    body: _ScopedRequest,
    principal: Principal | None = None,
    settings: Settings | None = None,
) -> MemoryScope:
    """Resolve the scope for a request body, and bind it to the log context.

    The tenant comes from `principal` when the key is bound to one — never from
    the body. That is the whole difference between a self-hosted deployment,
    where the caller owns everything, and a hosted one, where a request must not
    be able to name a tenant it does not own.
    """
    principal = principal or Principal.anonymous()
    client_id = principal.resolve_client_id(body.client_id)

    scope = build_scope(
        user_id=body.user_id,
        phone_number=body.phone_number,
        agent_id=body.agent_id or body.bot_id,
        client_id=client_id,
        namespace=body.namespace,
        settings=settings or get_settings(),
    )
    bind_scope(scope)
    return scope


async def scope_from_query(
    principal: PrincipalDep,
    user_id: Annotated[str | None, Query(alias="userId")] = None,
    phone_number: Annotated[str | None, Query(alias="phoneNumber")] = None,
    agent_id: Annotated[str | None, Query(alias="agentId")] = None,
    client_id: Annotated[str | None, Query(alias="clientId")] = None,
    bot_id: Annotated[str | None, Query(alias="botId")] = None,
    namespace: Annotated[str | None, Query(alias="namespace")] = None,
) -> MemoryScope:
    """Scope for GET and DELETE endpoints.

    Every read is scoped, including reads by id. A memory id is a random handle,
    not an authorisation token, so possessing one must not be enough to read the
    record.

    `namespace` travels as a JSON object — `?namespace={"env":"prod"}` — because
    a query string carries flat values and a partition map is not flat. Without
    it, a memory written into a namespace could be created and searched but
    never listed, fetched, audited or erased: every read here resolved to the
    un-namespaced partition next door and reported it as not found.
    """
    scope = build_scope(
        user_id=user_id,
        phone_number=phone_number,
        agent_id=agent_id or bot_id,
        client_id=principal.resolve_client_id(client_id),
        namespace=_parse_namespace(namespace),
    )
    bind_scope(scope)
    return scope


ScopeDep = Annotated[MemoryScope, Depends(scope_from_query)]


def _parse_namespace(raw: str | None) -> dict[str, str]:
    """Decode the `namespace` query parameter.

    Malformed input is a 422, never an empty namespace. Silently dropping it
    would read from a different partition than the caller named, and return an
    empty answer that looks like a real one.
    """
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValidationError(
            'namespace must be a JSON object, e.g. {"env":"prod"}',
            details={"namespace": raw[:200]},
        ) from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValidationError(
            "namespace must be a JSON object of string keys to string values",
            details={"namespace": raw[:200]},
        )
    return value
