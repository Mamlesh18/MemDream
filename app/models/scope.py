"""The tenancy scope every memory operation is bound to.

`MemoryScope` is constructed *once*, by the backend, from the authenticated
request — never by the LLM and never from anything a model returns. Everything
downstream (Mongo filters, FAISS partitions, Dream candidate sets) derives its
isolation from this object.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MemoryScope(BaseModel):
    """Immutable (client, bot, user) triple identifying one memory partition."""

    model_config = {"frozen": True}

    client_id: str = Field(min_length=1, max_length=128)
    bot_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    #: Salted hash of the originating phone number, when there was one. Stored
    #: so an operator can reconcile a user without the raw number being present.
    phone_number_hash: str | None = None

    def as_filter(self) -> dict[str, str]:
        """The Mongo filter fragment that pins a query to this scope."""
        return {
            "clientId": self.client_id,
            "botId": self.bot_id,
            "userId": self.user_id,
        }

    def as_log_fields(self) -> dict[str, str]:
        return {
            "client_id": self.client_id,
            "bot_id": self.bot_id,
            "user_id": self.user_id,
        }

    @property
    def partition_key(self) -> str:
        """Stable key for per-scope resources such as a FAISS index file."""
        return f"{self.client_id}/{self.bot_id}/{self.user_id}"

    def matches(self, document: dict) -> bool:
        """True when a raw Mongo document belongs to this scope.

        Used as a defence-in-depth assertion after reads: the query filter should
        already guarantee it, and a mismatch means a bug worth failing loudly on.
        """
        return (
            document.get("clientId") == self.client_id
            and document.get("botId") == self.bot_id
            and document.get("userId") == self.user_id
        )
