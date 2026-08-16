"""Deriving a stable, non-reversible user identity from a phone number.

The raw phone number is accepted at the API edge because that is what calling
applications have, and is then dropped: only ``userId`` and ``phoneNumberHash``
are persisted, and neither is ever written into memory *content*.

Both derivations are salted with ``DREAM_IDENTITY_SALT``. Changing that salt
re-partitions every user, so it must be set once and kept.
"""

from __future__ import annotations

import hashlib
import hmac
import re

from app.core.config import Settings, get_settings
from app.core.exceptions import ValidationError
from app.models.scope import MemoryScope

_NON_DIGITS = re.compile(r"[^\d+]")
_VALID_E164 = re.compile(r"^\+?[1-9]\d{6,17}$")


def normalize_phone_number(phone_number: str) -> str:
    """Normalise to a bare E.164-style string: leading ``+``, digits only.

    This is deliberately conservative — it does not guess country codes. Two
    spellings of the same number normalise together only if they differ by
    punctuation, spacing or a ``00`` international prefix.
    """
    if not phone_number or not phone_number.strip():
        raise ValidationError("phoneNumber must not be empty")

    cleaned = _NON_DIGITS.sub("", phone_number.strip())
    # "0091..." and "+91..." are the same number written two ways.
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    digits = cleaned[1:]
    if not digits.isdigit():
        raise ValidationError(f"phoneNumber is not a valid number: {phone_number!r}")
    if not _VALID_E164.match(cleaned):
        raise ValidationError(
            f"phoneNumber must be 7-18 digits in E.164 form, got {phone_number!r}"
        )
    return cleaned


def _digest(salt: str, *parts: str) -> str:
    """Salted HMAC-SHA256 over the parts, joined by a separator that cannot
    appear inside them (so ``a|b`` and ``ab|`` cannot collide)."""
    message = "\x1f".join(parts).encode("utf-8")
    return hmac.new(salt.encode("utf-8"), message, hashlib.sha256).hexdigest()


def hash_phone_number(phone_number: str, settings: Settings | None = None) -> str:
    """Salted hash of a normalised phone number, stable across tenants."""
    settings = settings or get_settings()
    normalized = normalize_phone_number(phone_number)
    return _digest(settings.identity_salt, "phone", normalized)[:32]


def derive_user_id(
    *,
    client_id: str,
    bot_id: str,
    phone_number: str,
    settings: Settings | None = None,
) -> str:
    """Derive a ``userId`` from a phone number, scoped to (client, bot).

    Scoping the hash means the same person contacting two different clients gets
    two unrelated identifiers, so identity cannot be correlated across tenants
    by comparing stored ids.
    """
    settings = settings or get_settings()
    normalized = normalize_phone_number(phone_number)
    digest = _digest(settings.identity_salt, "user", client_id, bot_id, normalized)
    return f"user_{digest[:24]}"


def build_scope(
    *,
    client_id: str,
    bot_id: str,
    phone_number: str | None = None,
    user_id: str | None = None,
    settings: Settings | None = None,
) -> MemoryScope:
    """Build the scope for a request.

    Callers supply either a ``phoneNumber`` (the common case — the framework
    derives the identity) or an already-normalised ``userId`` of their own. If
    both arrive, the explicit ``userId`` wins and the phone number is still
    hashed for reconciliation.
    """
    settings = settings or get_settings()
    client_id = (client_id or "").strip()
    bot_id = (bot_id or "").strip()
    if not client_id:
        raise ValidationError("clientId is required")
    if not bot_id:
        raise ValidationError("botId is required")

    phone_hash: str | None = None
    if phone_number:
        phone_hash = hash_phone_number(phone_number, settings)

    resolved = (user_id or "").strip()
    if not resolved:
        if not phone_number:
            raise ValidationError("Either phoneNumber or userId must be provided")
        resolved = derive_user_id(
            client_id=client_id,
            bot_id=bot_id,
            phone_number=phone_number,
            settings=settings,
        )

    return MemoryScope(
        client_id=client_id,
        bot_id=bot_id,
        user_id=resolved,
        phone_number_hash=phone_hash,
    )
