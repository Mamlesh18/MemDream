"""System prompts and the JSON payloads that accompany them.

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
# Extraction
# --------------------------------------------------------------------------- #

#: A one-line description of what this deployment is, injected into the
#: extraction and decision prompts. Empty by default — the prompts work without
#: it — but a single sentence of domain framing is the cheapest accuracy win
#: available: it tells the model that "no onions" is a standing constraint
#: rather than small talk, without a paragraph of rules trying to say so
#: generically.
#:
#:     DREAM_DOMAIN="A restaurant ordering assistant. Remember contact and
#:     delivery details, dietary needs, dislikes, and usual orders per meal."
DEFAULT_DOMAIN = ""


def _domain_block(domain: str) -> str:
    return f"\n\nContext: {domain.strip()}" if domain and domain.strip() else ""


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
#
# Every prompt below is deliberately terse. The rules are all load-bearing —
# most were added to fix a specific misbehaviour — but the *justifications* were
# not: a model follows "preferences are never permanent" just as well without
# three sentences explaining why. Cutting the prose took the write path from
# ~2150 prompt tokens to ~500 with no measured behaviour change.
#
# What remains is one rule per line, imperative, no hedging. Field semantics are
# stated here rather than in the JSON schema, because `schema_hint` strips
# descriptions to stop the model echoing documentation back as values.

EXTRACTION_SYSTEM = """\
Extract facts about the user that are still true next month.{domain}

One fact per memory, third person about "User", self-contained months later:
resolve pronouns, keep names, numbers, dates. Split a message stating several.

Likes and dislikes both count. When unsure, extract — importance ranks a
memory, it never drops one. Never infer past what was said; "I might move" is a
plan, not a move.

`temporalNature` is `current` unless the fact cannot change by nature — a
birthplace or a date of birth. Addresses, phones, jobs, preferences, habits and
orders all change, so they are `current`: `permanent` protects a memory from
ever being corrected.

`sensitive` discards the memory, and is only for payment details, passwords and
government ids. Names, phones, addresses, allergies and dietary needs are
ordinary memories.

Skip anything not about this user. Finding nothing is normal: hasMemory false.\
"""


# --------------------------------------------------------------------------- #
# Shared rule blocks
# --------------------------------------------------------------------------- #

_SUPERSEDE_RULES = """\
SUPERSEDE only when the new fact makes an old one untrue — not because it is
newer, related, or on the same topic. Added detail is MERGE. Different subjects
never conflict (two people, two dishes). Historical facts survive: "worked at
Acme until 2024" and "works at Globex" are both true. Permanent facts are never
replaced by current ones.
targetMemoryIds: every memory replaced and only those, ids exactly as given.
supersededValidUntil: when the old fact stopped being true, if knowable.\
"""

_MERGE_RULES = """MERGE when the new memory duplicates an existing one, restates it in other
words, or adds detail. Return the most complete phrasing as memory.content,
losing no detail either input had. Same topic is not the same fact — two
preferences stay two memories. Never merge different subjects. Gain nothing,
return NOOP.
targetMemoryIds: the memories folded in, ids exactly as given.\
"""

_SHARED_CLOSING = """\
You recommend; the backend executes, drops ids it did not give you, and
deletes nothing. Give a `reason` naming the facts.\
"""


#: The two rule sets overlap on three rules - subjects, history, id
#: handling. The staged prompts state them separately because each runs
#: alone; the unified prompt states them once and drops the repetition.
_UNIFIED_RULES = """\
SUPERSEDE when the new fact makes an old one untrue: a changed address,
city, job, phone or preference. Never on topic alone, and added detail is
MERGE, not SUPERSEDE. Two distinct preferences stay two memories. Neither applies across different subjects (two
people, two dishes) and neither rewrites history: "worked at Acme until 2024"
and "works at Globex" are both true. Permanent facts are never replaced.
Unsure between ADD and SUPERSEDE, choose ADD — a wrongly retired memory hides
the truth. Unsure between MERGE and NOOP, choose NOOP.
For MERGE return the most complete phrasing, losing no detail either input had.
targetMemoryIds: the memories replaced or folded in, ids exactly as given.\
"""


DECISION_SYSTEM = """\
Decide what to do with one new memory against the existing ones offered.{domain}

ADD — new; nothing conflicts or duplicates.
SUPERSEDE — the new fact makes an old one untrue.
MERGE — duplicates, restates or enriches an existing memory.
NOOP — already stored, in equal or better form.

{rules}
memory: the candidate for ADD, the new fact for SUPERSEDE, the canonical text
for MERGE, null for NOOP.

{closing}\
"""


SUPERSEDE_SYSTEM = """\
Decide whether a new memory contradicts existing memories and replaces them.
Return SUPERSEDE or NOOP only — another stage handles ADD and MERGE.{domain}

{supersede}

{closing}\
"""


MERGE_SYSTEM = """\
Decide whether a new memory duplicates or enriches existing memories and should
be consolidated. Return MERGE or NOOP only — another stage handles ADD and
SUPERSEDE.{domain}

{merge}

Example — existing "User owns a dog named Rex.", new "Rex is three years old
and a Golden Retriever." Canonical: "User owns a three-year-old Golden Retriever
named Rex." Both inputs survive in it.

{closing}\
"""


# --------------------------------------------------------------------------- #
# Synthesis
# --------------------------------------------------------------------------- #

SYNTHESIS_SYSTEM = """\
Decide whether a cluster of a user's memories reveals a pattern none of them
states alone — a habit, a consistent preference, a trajectory. Restating the
inputs is not a pattern.{domain}

SYNTHESIZE only when all hold:
- At least three supplied memories support it.
- It says something no single memory says.
- The evidence is consistent, not half agreeing and half contradicting.
- It stays useful if the individual memories are forgotten.

NOOP when the evidence is thin or coincidental, when memories are merely
related, or when an existing pattern already covers it. Check existingPatterns
by wording *and* sourceMemoryIds — a near-duplicate pattern is the worst
outcome here.

Writing it:
- One or two sentences, third person about "User".
- Concrete: "User orders vegetarian at dinner but not at lunch", not "User has
  food preferences".
- Assert nothing the evidence does not show. No personality, motive or
  diagnosis.
- sourceMemoryIds: every supporting memory, ids exactly as given. A pattern
  without sources is rejected.

You recommend only. Source memories are never modified.\
"""


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def extraction_system(domain: str = DEFAULT_DOMAIN) -> str:
    return EXTRACTION_SYSTEM.format(domain=_domain_block(domain))


def decision_system(domain: str = DEFAULT_DOMAIN) -> str:
    return DECISION_SYSTEM.format(
        domain=_domain_block(domain), rules=_UNIFIED_RULES, closing=_SHARED_CLOSING
    )


def supersede_system(domain: str = DEFAULT_DOMAIN) -> str:
    return SUPERSEDE_SYSTEM.format(
        domain=_domain_block(domain), supersede=_SUPERSEDE_RULES, closing=_SHARED_CLOSING
    )


def merge_system(domain: str = DEFAULT_DOMAIN) -> str:
    return MERGE_SYSTEM.format(
        domain=_domain_block(domain), merge=_MERGE_RULES, closing=_SHARED_CLOSING
    )


def synthesis_system(domain: str = DEFAULT_DOMAIN) -> str:
    return SYNTHESIS_SYSTEM.format(domain=_domain_block(domain))


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
