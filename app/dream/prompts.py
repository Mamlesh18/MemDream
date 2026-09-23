"""The JSON payloads that accompany a prompt.

The prompts themselves are **not here**: MemDream ships none, and a deployment
supplies its own through configuration. See `app/core/prompts.py`.

What stays here is the other half of every model call — the user message. It is
always a single JSON object built from memories, which is what lets a custom
prompt be written against a stable input shape.

Two conventions hold throughout:

* **The system prompt carries the rules; the user message carries only data.**
  Every user message is a single JSON object. That keeps instructions and data
  separated (a memory whose text reads "ignore previous instructions" is data,
  not an instruction), and it lets the offline `FakeLLMProvider` read exactly the
  same input the real model does.

* **Ids are opaque handles.** The model is told to echo back ids from the payload
  and never to invent them; the engine then discards any id it did not supply.
  The prompt asks for cooperation, the backend enforces it.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.models.memory import Memory, utcnow

# --------------------------------------------------------------------------- #
# Payload builders
# --------------------------------------------------------------------------- #


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _memory_view(memory: Memory, similarity: float | None = None) -> dict[str, Any]:
    """The projection of a memory the model is allowed to see.

    Tenancy fields are deliberately absent: the model has no business knowing
    which client or user this is, and cannot reason about scope it never sees.

    Everything else here earns its place, because this is the dominant cost of a
    decision — ten candidates at the old 88 tokens each was 880 tokens of
    payload against a 380-token prompt. What went:

        importance, confidence, explicit   ranking signals the *backend* uses.
                                           The model was never asked to weigh
                                           them and has no rule that reads them.
        similarity                         the reason this candidate was
                                           offered, not evidence about it.
                                           Showing it invites the model to
                                           treat "similar" as "contradicts".
        full ISO timestamps                the rules compare *order*, not
                                           instants. A date is enough.

    What stayed is what a rule actually reads: the text, the id to echo back,
    the type (different subjects never conflict), the status and temporal nature
    (permanent facts are never replaced), and the validity window.
    """
    view: dict[str, Any] = {
        "id": memory.memory_id,
        "text": memory.content,
        "type": memory.memory_type,
        "temporal": memory.temporal_nature.value,
        "created": _date(memory.created_at),
    }
    # Only when they say something. An active memory with an open-ended validity
    # window is the common case, and three nulls per candidate is 30 tokens of
    # nothing across a full candidate set.
    if memory.status.value != "active":
        view["status"] = memory.status.value
    if memory.valid_from:
        view["from"] = _date(memory.valid_from)
    if memory.valid_until:
        view["until"] = _date(memory.valid_until)
    return view


def _date(value: datetime | None) -> str | None:
    """Just the day. The rules compare order, never instants, and a full
    timestamp costs three times as much to say."""
    return value.date().isoformat() if value else None


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def build_extraction_payload(
    message: str, *, now: datetime | None = None, message_id: str | None = None
) -> str:
    return _dumps(
        {
            "task": "extract",
            # Anchors "last month" and friends to a real date.
            "now": _iso(now or utcnow()),
            "message": message,
            "messageId": message_id,
        }
    )


def build_decision_payload(
    candidate: dict[str, Any],
    related: list[tuple[Memory, float]],
    *,
    now: datetime | None = None,
    task: str = "decide",
) -> str:
    return _dumps(
        {
            "task": task,
            "now": _iso(now or utcnow()),
            "candidate": candidate,
            "existingMemories": [_memory_view(memory) for memory, _ in related],
        }
    )


def build_synthesis_payload(
    memories: list[Memory],
    existing_patterns: list[Memory],
    *,
    now: datetime | None = None,
) -> str:
    return _dumps(
        {
            "task": "synthesize",
            "now": _iso(now or utcnow()),
            "memories": [_memory_view(memory) for memory in memories],
            "existingPatterns": [
                {
                    "memoryId": pattern.memory_id,
                    "content": pattern.content,
                    "sourceMemoryIds": pattern.source_memory_ids,
                    "confidence": round(pattern.confidence, 3),
                }
                for pattern in existing_patterns
            ],
        }
    )
