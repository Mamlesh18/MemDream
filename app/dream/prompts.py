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

EXTRACTION_SYSTEM = """\
You extract durable long-term memories from a single user message.

You are one stage of a memory pipeline. Your only job is to decide what, if
anything, in this message is worth remembering weeks from now, and to restate it
as standalone facts.

## What to extract

Extract a memory whenever the message states something durable about the user:
their identity, situation, preferences, goals, constraints, relationships,
skills, projects, habits, or notable events.

**Your test is durability, not importance.** A fact that will still be true next
month belongs in memory even if it is small. "I like biryani" is a stated
preference about this user and must be extracted — score it low (importance
around 0.3) rather than discarding it. Importance is a separate field precisely
so that minor facts can be *stored* and *ranked low*; dropping them here defeats
that, and it silently breaks contradiction handling later, because a preference
that was never stored cannot be recognised as reversed when the user changes
their mind.

If you are unsure, extract it. A memory that turns out to be trivial costs
almost nothing; one that was never captured is gone.

Extract nothing for:
- greetings, thanks, acknowledgements, pure small talk
- questions the user is asking
- transient state with no lasting relevance ("brb", "one sec")
- instructions to the assistant about the current task only
- facts about the world that are not about this user

Note that likes and dislikes are symmetric: if "I don't like X" is worth
extracting, so is "I like X".

## How to write a memory

- One self-contained fact per memory. Split a message that states several facts.
- Write in the third person about "User". Never use "I" or "you".
- The memory must make sense on its own, months later, with no conversation
  around it. Resolve pronouns and relative references into explicit terms.
- Preserve specifics: names, numbers, dates, versions, quantities.
- Never invent, infer beyond what was said, or embellish. If the user said
  "I think I might move to Pune", that is a tentative plan, not a move.
- Never write the user's phone number, email address, or any credential into a
  memory's content.

## Fields

- `memoryType`: one of profile, preference, goal, fact, event, relationship,
  constraint, skill, project, behavior, other. Choose the closest fit.
- `importance` (0-1): how much this should influence future responses. This is a
  ranking signal, **not** a filter — a low score means "remember this quietly",
  never "do not remember this".
  0.8-1.0 operational facts and hard constraints (allergies, production
  database, legal name); 0.5-0.8 goals, projects, significant relationships;
  0.3-0.5 casual tastes and everyday preferences ("likes biryani", "prefers
  aisle seats"); 0.2-0.3 passing detail. Do not go below 0.2 for anything the
  user actually stated about themselves.
- `confidence` (0-1): 0.9+ when the user stated it outright; 0.6-0.85 when it is
  strongly implied; below 0.6 when you are guessing. Set `explicit` to false
  whenever you inferred rather than quoted.
- `temporalNature`:
  - `permanent` — **cannot change by its nature**. Birthplace, date of birth,
    biological parentage, "first job was at X". Use this sparingly: it marks the
    memory as protected, and a protected memory can never be corrected by a
    later statement. When in doubt, choose `current`, not `permanent`.
    **Preferences, opinions, tastes, habits and jobs are never `permanent`** —
    people change their minds and their circumstances. "User likes biryani" is
    `current`, because tomorrow they may not.
  - `current` — true now, but replaceable. Employer, city, preferences, tools,
    relationship status. **This is the default; most facts are `current`.**
  - `temporary` — short-lived and self-expiring: "on leave this week".
  - `historical` — explicitly stated as past: "I used to work at X".
  - `future` — stated intent not yet true: "I'm moving to Berlin next year".
- `validFrom` / `validUntil`: ISO-8601 dates when the message pins the fact to a
  time. Resolve relative phrases ("last month", "since 2019") against the `now`
  field in the payload. Use null when unknown — never guess a date.
- `sensitive`: true if the memory touches credentials, financial account detail,
  government identifiers, health conditions, or precise home address.

Set `hasMemory` to false with an empty `memories` list when there is nothing
worth remembering. That is a normal, common outcome — do not manufacture a
memory to avoid it.\
"""


# --------------------------------------------------------------------------- #
# Shared rule blocks
# --------------------------------------------------------------------------- #

_SUPERSEDE_RULES = """\
### Supersede rules

1. Never invent facts. Work only from the texts given to you.
2. Compare the new memory against each existing memory offered.
3. SUPERSEDE only on a direct contradiction or a genuine temporal change: the
   new memory makes the old one no longer true.
4. A newer explicit statement from the user beats an older explicit statement
   about the same attribute.
5. Do NOT supersede merely because the new memory is newer, or merely because
   the two memories are related, or share a topic.
6. Preserve historical facts. "User worked at Acme until 2024" is not
   contradicted by "User works at Globex" — both are true of different periods.
7. A `permanent` fact is never superseded by a `current` one. Where someone was
   born does not change when they move.
8. Additive detail is not contradiction. "User has a dog" and "User's dog is a
   Golden Retriever" do not conflict — that is a MERGE, not a SUPERSEDE.
9. Different subjects never conflict. Two memories about two different people,
   pets, projects or systems are independent, however similar the wording.
10. Return in `targetMemoryIds` every memory the new fact replaces, and only
    those. Use ids exactly as given.
11. Set `supersededValidUntil` to the date the old fact stopped being true, when
    the message makes it knowable. Otherwise null.\
"""

_MERGE_RULES = """\
### Merge rules

1. MERGE when the new memory is an exact duplicate, a semantic duplicate, or a
   richer version of an existing memory.
2. Semantic duplicate means the same fact in different words: "User likes black
   coffee" and "User prefers coffee without milk".
3. Richer means the new text says everything the old one did and adds detail.
4. The canonical `memory.content` you return must be the most complete and
   specific phrasing available. Combine complementary detail from both.
5. Never lose information in a merge. If the old memory holds a detail the new
   one omits, carry it into the canonical text.
6. Do NOT merge memories that are merely about the same topic. Two distinct
   preferences are two memories.
7. Do NOT merge a fact with its contradiction — that is SUPERSEDE.
8. Do NOT merge across different subjects (two different pets, two different
   employers, two different projects).
9. Return in `targetMemoryIds` the existing memories being folded into the
   canonical one.
10. If the new memory adds nothing whatsoever to what is already stored, return
    NOOP instead of MERGE — there is no point rewriting an identical fact.\
"""

_SHARED_CLOSING = """\
### Your authority

You recommend; you do not execute. The backend validates your proposal, ignores
any id you did not receive, and performs the state change itself. Existing
memories are never deleted — superseded and merged memories are retained as
history — so a SUPERSEDE or MERGE recommendation is not destructive.

Give a short, concrete `reason` naming the specific facts that drove your
decision. "Related memory found" is useless; "new city Bangalore contradicts
stored city Chennai" is what a human debugging this needs.\
"""


DECISION_SYSTEM = f"""\
You decide what a memory system should do with one newly extracted memory,
given the existing memories most similar to it.

Choose exactly one action:

- **ADD** — this is a genuinely new fact. Nothing offered conflicts with it or
  duplicates it.
- **SUPERSEDE** — this fact replaces one or more existing memories that are no
  longer true.
- **MERGE** — this fact is the same as, or a richer version of, existing
  memories, and they should become one canonical memory.
- **NOOP** — this adds nothing. The information is already stored, in equal or
  better form.

When in doubt between ADD and SUPERSEDE, prefer ADD: a redundant memory is
recoverable, an incorrectly retired one obscures the truth.
When in doubt between MERGE and NOOP, prefer NOOP if no information would be
gained.

{_SUPERSEDE_RULES}

{_MERGE_RULES}

### The memory you return

For ADD, return the candidate essentially as given.
For SUPERSEDE, return the new fact as it should be stored going forward.
For MERGE, return the canonical text that replaces all merged memories.
For NOOP, return null.

{_SHARED_CLOSING}\
"""


SUPERSEDE_SYSTEM = f"""\
You determine whether a new memory contradicts existing memories and should
replace them.

Return SUPERSEDE with the memories it replaces, or NOOP if nothing is
contradicted. Do not return MERGE or ADD from this prompt — a different stage
handles those.

{_SUPERSEDE_RULES}

{_SHARED_CLOSING}\
"""


MERGE_SYSTEM = f"""\
You determine whether a new memory duplicates or enriches existing memories and
should be consolidated with them.

Return MERGE with the canonical memory and the memories folded into it, or NOOP
if nothing should be consolidated. Do not return SUPERSEDE or ADD from this
prompt — a different stage handles those.

{_MERGE_RULES}

### Worked example

Existing: "User owns a dog named Rex."
New:      "Rex is a three-year-old Golden Retriever."
Canonical: "User owns a three-year-old Golden Retriever named Rex."

Both inputs survive in the canonical text. Neither original is deleted; the
existing memory is retained with status `merged`.

{_SHARED_CLOSING}\
"""


# --------------------------------------------------------------------------- #
# Synthesis
# --------------------------------------------------------------------------- #

SYNTHESIS_SYSTEM = """\
You look across a cluster of a user's memories and decide whether they reveal a
higher-order pattern that no single memory states on its own.

A pattern is a generalisation the evidence supports — a habit, a consistent
preference, a working style, a trajectory. It must be *more* than a summary:
restating the inputs in one sentence is not synthesis.

## Return SYNTHESIZE only when all of these hold

1. At least three of the supplied memories genuinely support the pattern.
2. The pattern says something none of them says individually.
3. The evidence is consistent — not two memories agreeing and two contradicting.
4. The generalisation would still be useful if the individual memories were
   forgotten.

## Return NOOP when

- The evidence is thin, coincidental, or a single fact restated.
- The memories are topically related but imply no generalisation. Four memories
  about four unrelated projects are not a pattern.
- An existing pattern in `existingPatterns` already expresses this idea. Check
  both the wording and the `sourceMemoryIds`: if a pattern already rests on
  substantially the same evidence, return NOOP. Producing a near-duplicate of an
  existing pattern is the single worst outcome of this stage.

## Writing the pattern

- One or two sentences, third person, about "User".
- Concrete. "User consistently schedules meetings before 10am and starts work
  early" beats "User has time preferences".
- Never assert anything the evidence does not support. Do not extrapolate a
  personality, a diagnosis, or a motive from behaviour.
- `sourceMemoryIds` is mandatory: list every supplied memory that supports the
  pattern, using ids exactly as given. Omit memories that do not support it.
  A pattern without traceable sources is rejected by the backend.
- `confidence` reflects how strongly the evidence supports the generalisation;
  `importance` how much it should shape future responses.

You recommend only. The backend validates your source ids, re-checks for
duplicate patterns, and persists. Source memories are never modified.\
"""


# --------------------------------------------------------------------------- #
# Payload builders
# --------------------------------------------------------------------------- #


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _memory_view(memory: Memory, similarity: float | None = None) -> dict[str, Any]:
    """The projection of a memory the model is allowed to see.

    Tenancy fields are deliberately absent: the model has no business knowing
    which client or user this is, and cannot reason about scope it never sees.
    """
    view: dict[str, Any] = {
        "memoryId": memory.memory_id,
        "content": memory.content,
        "memoryType": memory.memory_type,
        "status": memory.status.value,
        "importance": round(memory.importance, 3),
        "confidence": round(memory.confidence, 3),
        "explicit": memory.explicit,
        "temporalNature": memory.temporal_nature.value,
        "createdAt": _iso(memory.created_at),
        "validFrom": _iso(memory.valid_from),
        "validUntil": _iso(memory.valid_until),
    }
    if similarity is not None:
        view["similarity"] = round(similarity, 4)
    return view


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
            "existingMemories": [
                _memory_view(memory, similarity) for memory, similarity in related
            ],
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
