"""The tenancy scope every memory operation is bound to.

`MemoryScope` is constructed *once*, by the backend, from the authenticated
request — never by the LLM and never from anything a model returns. Everything
downstream (Mongo filters, vector partitions, Dream candidate sets) derives its
isolation from this object.

## One required field

`user_id` is the isolation boundary. Everything else is optional:

    MemoryScope(user_id="alice")                          # simplest
    MemoryScope(user_id="alice", agent_id="support")      # one agent among several
    MemoryScope(user_id="alice", agent_id="support", client_id="acme")   # SaaS
    MemoryScope(user_id="alice", namespace={"env": "prod"})              # anything else

That ordering is deliberate. A solo developer with one agent has no "client" and
no "bot", and forcing them to invent `client_id="default"` is friction for no
benefit. A multi-tenant platform adds the dimensions it actually has.

## How partitions are computed

Every dimension contributes to `scope_key`, a stable digest of the whole scope.
Two scopes are the same partition when every dimension matches — so adding
`agent_id` to an existing deployment creates a *new* partition rather than
silently widening the old one.

Filtering is on that single indexed key rather than a multi-field predicate.
With optional fields a predicate has to reason about null-versus-missing for
each one, and getting that wrong is how a query quietly matches the wrong
partition. One equality check on a hash cannot be got subtly wrong.

## The safety note that matters

With `client_id` optional, **`user_id` alone can become the boundary**. If two
different customers both write `user_id="user_123"` with no other dimension,
they are the same partition and will see each other's memories.

`build_scope()` protects against this when identity is derived from a phone
number: the derivation is salted with whatever dimensions were supplied, so the
same number under two clients yields two unrelated ids. Callers supplying raw
user ids own that guarantee themselves — namespace them, or supply `client_id`.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MemoryScope(BaseModel):
    """Immutable identifier for one memory partition."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    #: The isolation boundary. The only required dimension.
    user_id: str = Field(min_length=1, max_length=128)

    #: Which agent or bot this memory belongs to. Optional — omit it when a
    #: deployment has only one.
    agent_id: str | None = Field(default=None, max_length=128)

    #: The tenant, for platforms serving several customers. Optional.
    client_id: str | None = Field(default=None, max_length=128)

    #: Any further dimensions a deployment wants to partition on — environment,
    #: region, workspace, channel. Keys and values are opaque to MemDream.
    namespace: dict[str, str] = Field(default_factory=dict)

    #: Salted hash of the originating phone number, when there was one. Stored
    #: so an operator can reconcile a user without the raw number being present.
    phone_number_hash: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_bot_id(cls, data):
        """`bot_id` is the previous name for `agent_id`; keep it working.

        Renaming a field in a released API is not worth breaking every caller
        over, so the old spelling is accepted and mapped.
        """
        if isinstance(data, dict) and "bot_id" in data:
            data = dict(data)
            bot_id = data.pop("bot_id")
            data.setdefault("agent_id", bot_id)
            if "botId" in data:
                data.pop("botId")
        return data

    # --- identity ----------------------------------------------------------- #

    @property
    def partition_key(self) -> str:
        """Human-readable identifier for this partition.

        Dimensions are emitted in a fixed order and namespace keys are sorted,
        so the same logical scope always produces the same string regardless of
        how it was constructed.
        """
        parts = [f"user={self.user_id}"]
        if self.agent_id:
            parts.append(f"agent={self.agent_id}")
        if self.client_id:
            parts.append(f"client={self.client_id}")
        for key in sorted(self.namespace):
            parts.append(f"{key}={self.namespace[key]}")
        return "|".join(parts)

    @property
    def scope_key(self) -> str:
        """Stable digest of the whole scope. The partition identity.

        Persisted on every memory and used as the sole filter, so a query is one
        indexed equality check rather than a predicate over optional fields.
        """
        return hashlib.sha256(self.partition_key.encode("utf-8")).hexdigest()[:32]

    # --- persistence -------------------------------------------------------- #

    def as_filter(self) -> dict[str, str]:
        """The Mongo filter fragment that pins a query to this scope."""
        return {"scopeKey": self.scope_key}

    def as_document_fields(self) -> dict:
        """Scope fields to persist on a memory.

        The individual dimensions are stored alongside `scopeKey` even though
        filtering never uses them — they make the data legible to an operator
        querying by hand, and support cross-partition reporting such as "every
        agent belonging to this client".
        """
        fields: dict = {
            "scopeKey": self.scope_key,
            "userId": self.user_id,
            "agentId": self.agent_id,
            "clientId": self.client_id,
        }
        if self.namespace:
            fields["namespace"] = dict(self.namespace)
        return fields

    def as_model_fields(self) -> dict:
        """Tenancy kwargs for constructing a Memory, relation, operation or job.

        Everything built from a scope goes through this, so a new dimension can
        never be silently dropped at one construction site and land the row in a
        partition its own filter will never match.
        """
        return {
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "client_id": self.client_id,
            "namespace": dict(self.namespace),
            "scope_key": self.scope_key,
        }

    def as_log_fields(self) -> dict[str, str]:
        fields = {"user_id": self.user_id}
        if self.agent_id:
            fields["agent_id"] = self.agent_id
        if self.client_id:
            fields["client_id"] = self.client_id
        return fields

    def matches(self, document: dict) -> bool:
        """True when a raw Mongo document belongs to this scope.

        Used as a defence-in-depth assertion after reads: the query filter should
        already guarantee it, and a mismatch means a bug worth failing loudly on.
        """
        stored = document.get("scopeKey")
        if stored is not None:
            return stored == self.scope_key

        # Documents written before scopeKey existed. Fall back to comparing the
        # dimensions directly so a mid-migration deployment still reads its own
        # history rather than reporting an isolation violation.
        return (
            document.get("userId") == self.user_id
            and document.get("agentId") == self.agent_id
            and document.get("clientId") == self.client_id
        )

    # --- compatibility ------------------------------------------------------- #

    @property
    def bot_id(self) -> str | None:
        """Previous name for `agent_id`."""
        return self.agent_id
