"""API keys, and the principal a request acts as.

## The problem this solves

Until now `clientId` arrived in the request body. For a self-hosted deployment
that is correct — the caller owns the whole installation, and there is nobody to
impersonate. For a *hosted* service it is a critical vulnerability: any valid key
could name any `clientId` and read another customer's memories.

The fix is structural. A key can be **bound** to a client, and for a bound key
the tenant comes from the key, never from the request. A body that names a
different client is rejected outright rather than silently overridden — a silent
override would hide both the bug and the attack.

## Three modes, chosen by configuration

    open        DREAM_API_KEYS empty
                No authentication. Correct behind a private gateway, and the
                reason auth is opt-in rather than mandatory.

    shared      DREAM_API_KEYS=key1,key2
                Keys authenticate but carry no tenant. `clientId` still comes
                from the body. Right for single-tenant behind your own auth.

    bound       DREAM_API_KEY_CLIENTS=key1:acme,key2:globex
                or an `api_keys` collection in MongoDB.
                The key determines the tenant. Required for hosting.

Set `DREAM_API_KEY_REQUIRE_BINDING=true` to refuse unbound keys entirely, so a
leftover shared secret cannot be used to reach another tenant.

## Storage

Keys are compared as SHA-256 digests and never stored in plaintext by the Mongo
path. Comparison is constant-time: a timing side channel on an auth check is
cheap to avoid and expensive to discover later.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.core.exceptions import UnauthorizedError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: How long a resolved key is trusted before Mongo is consulted again. Short
#: enough that revoking a key takes effect promptly, long enough that the hot
#: path is not a database round trip per request.
_CACHE_TTL_SECONDS = 60.0


def hash_key(api_key: str) -> str:
    """The digest a key is stored and compared as."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    """Who a request is acting as.

    `bound` is the security-relevant field: when true the tenant came from the
    credential and the request may not name a different one.
    """

    client_id: str | None = None
    key_id: str | None = None
    bound: bool = False
    authenticated: bool = False

    @classmethod
    def anonymous(cls) -> Principal:
        """No authentication configured. The caller owns the deployment."""
        return cls(authenticated=False)

    def resolve_client_id(self, requested: str | None) -> str | None:
        """Decide the tenant for this request.

        For a bound key the answer is the key's client, and a request naming a
        different one is refused. Refusing rather than overriding matters: a
        silent override turns both an integration bug and a deliberate probe
        into something invisible.
        """
        if not self.bound:
            return requested

        if requested and requested != self.client_id:
            logger.warning(
                "client_id_mismatch",
                key_id=self.key_id,
                bound_client=self.client_id,
                requested_client=requested,
            )
            raise UnauthorizedError(
                "This API key is bound to a different client. Omit clientId, or "
                "send the one this key belongs to."
            )
        return self.client_id


class ApiKeyRegistry:
    """Resolves a presented key to a `Principal`.

    Looks in two places: static bindings from configuration, then an optional
    MongoDB collection. Configuration is checked first because it needs no
    round trip; Mongo exists so keys can be issued and revoked without a restart,
    which any real hosted service needs.
    """

    def __init__(self, settings: Settings, database: Any = None) -> None:
        self._settings = settings
        self._database = database
        self._cache: dict[str, tuple[Principal, float]] = {}

        # DREAM_API_KEY_CLIENTS=key1:acme,key2:globex
        self._static: dict[str, tuple[str, str]] = {}
        for entry in settings.api_key_clients:
            key, separator, client_id = entry.partition(":")
            if not separator or not key.strip() or not client_id.strip():
                logger.error("malformed_api_key_binding", entry=entry[:12] + "...")
                continue
            self._static[hash_key(key.strip())] = (client_id.strip(), f"static:{client_id.strip()}")

        # Unbound shared secrets.
        self._shared = {hash_key(key) for key in settings.api_keys}

    @property
    def enabled(self) -> bool:
        """True when any credential is configured at all."""
        return bool(self._shared or self._static or self._database is not None)

    async def resolve(self, api_key: str | None) -> Principal:
        if not self._settings.auth_enabled:
            return Principal.anonymous()

        if not api_key:
            raise UnauthorizedError("Missing X-API-Key header")

        digest = hash_key(api_key)

        cached = self._cache.get(digest)
        if cached and cached[1] > time.monotonic():
            return cached[0]

        principal = await self._lookup(digest)
        self._cache[digest] = (principal, time.monotonic() + _CACHE_TTL_SECONDS)
        return principal

    async def _lookup(self, digest: str) -> Principal:
        # 1. Static bindings. Constant-time comparison against each, rather than
        #    a dict hit, so lookup time does not reveal which keys exist.
        for known, (client_id, key_id) in self._static.items():
            if hmac.compare_digest(digest, known):
                return Principal(
                    client_id=client_id, key_id=key_id, bound=True, authenticated=True
                )

        # 2. MongoDB, for keys issued at runtime.
        if self._database is not None:
            record = await self._database.db["api_keys"].find_one(
                {"keyHash": digest, "active": True}
            )
            if record is not None:
                return Principal(
                    client_id=record.get("clientId"),
                    key_id=record.get("_id"),
                    # A row without a clientId authenticates but binds nothing.
                    bound=bool(record.get("clientId")),
                    authenticated=True,
                )

        # 3. Unbound shared secrets.
        for known in self._shared:
            if hmac.compare_digest(digest, known):
                if self._settings.api_key_require_binding:
                    raise UnauthorizedError(
                        "This key is not bound to a client, and unbound keys are "
                        "refused by this deployment."
                    )
                return Principal(authenticated=True, bound=False, key_id="shared")

        raise UnauthorizedError("Invalid API key")

    def invalidate(self, api_key: str | None = None) -> None:
        """Drop cached resolutions, so a revocation takes effect immediately."""
        if api_key is None:
            self._cache.clear()
        else:
            self._cache.pop(hash_key(api_key), None)
