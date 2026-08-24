"""An offline, deterministic LLM provider.

Three modes, checked in order:

1. **Scripted** — ``push(payload)`` queues exact responses. Use this to test how
   the engine handles a *specific* decision, including malformed ones.
2. **Registered handler** — ``register(task, fn)`` installs a callable for one
   task type.
3. **Heuristic** — a rule-based baseline that actually attempts the task.

The heuristic mode is the interesting one. A stub returning "ADD" for everything
would let every test pass without exercising supersede, merge or synthesis, so
this implements real (if crude) reasoning: token-overlap similarity, change-marker
detection for contradictions, and source-overlap checks for pattern duplicates.
It is good enough to run the whole evaluation suite offline and to make CI
meaningful; it is not good enough to ship in front of users.

The heuristics read the same JSON payload the real model receives, so prompt
changes are exercised by both paths.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from app.core.exceptions import LLMResponseError
from app.core.metrics import (
    LLM_CALLS,
    LLM_COMPLETION_TOKENS,
    LLM_LATENCY,
    LLM_PROMPT_TOKENS,
    LLM_TOKENS,
    current_operation,
    metrics,
)
from app.providers.llm.base import LLMProvider, LLMResult, validate_response

T = TypeVar("T", bound=BaseModel)

_TOKEN = re.compile(r"[a-z0-9']+")

_STOPWORDS = frozenset(
    ["a", "an", "the", "and", "or", "but", "if", "then", "than", "that", "this", "these", "those", "of", "in", "on", "at", "to", "for", "with", "from", "by", "as", "is", "are", "was", "were", "be", "been", "being", "am", "do", "does", "did", "have", "has", "had", "having", "i", "me", "my", "mine", "you", "your", "he", "him", "his", "she", "her", "it", "its", "we", "us", "our", "they", "them", "their", "will", "would", "shall", "should", "can", "could", "may", "might", "must", "not", "no", "so", "very", "just", "about", "into", "over", "under", "user", "users", "user's", "users'", "also", "always", "often", "usually"]
)
# Every memory is written in the third person about "User", so the subject word
# carries no signal — but it appears in both texts and inflates every overlap
# score it touches. Dropping it is what lets two memories be compared on what
# they actually say.

#: Words that mark a fact replacing an earlier one rather than adding to it.
_CHANGE_MARKERS = (
    "moved",
    "relocated",
    "switched",
    "changed",
    "migrated",
    "now ",
    "no longer",
    "instead of",
    "used to",
    "quit",
    "left ",
    "joined",
    "upgraded",
    "replaced",
    "new job",
    "started working",
    "stopped",
    "anymore",
)

#: Change frames: (markers that signal a change, tokens naming the attribute
#: being changed).
#:
#: This exists because the clearest supersede cases share almost no vocabulary
#: with what they replace — "User lives in Chennai" and "User moved to Bangalore"
#: have zero token overlap — so a similarity-based rule cannot see them at all.
#: Matching the *frame* instead ("moved" changes where someone lives) recovers
#: those cases without the rule degenerating into "any sentence containing
#: 'moved' replaces the nearest memory", which would happily retire the user's
#: name because a message mentioned moving house.
_CHANGE_FRAMES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("moved", "relocated", "shifted", "moving"),
        ("live", "lives", "living", "lived", "based", "reside", "resides", "city", "home", "located"),
    ),
    (
        ("switched", "migrated", "replaced", "upgraded", "changed", "now using", "moved to"),
        ("use", "uses", "using", "used", "runs", "running", "stack", "database", "db", "tool", "prefers", "prefer"),
    ),
    (
        ("joined", "left", "quit", "resigned", "new job", "started working", "hired"),
        ("work", "works", "working", "worked", "employed", "employer", "company", "job", "role", "position"),
    ),
    (
        ("no longer", "stopped", "gave up", "quit", "anymore"),
        ("does", "do", "plays", "play", "goes", "attends", "practises", "practices", "eats", "drinks"),
    ),
)

#: Keyword -> memory type. First match wins, so order matters.
_TYPE_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("prefer", "likes", "like ", "favourite", "favorite", "hate", "dislike", "rather"), "preference"),
    (("goal", "want to", "aiming", "training for", "plan to", "hope to", "target"), "goal"),
    (("lives", "live in", "moved", "based in", "born", "age", "years old", "name is"), "profile"),
    (("must", "cannot", "can't", "never", "allergic", "avoid", "restricted", "deadline"), "constraint"),
    (("knows", "skilled", "expert", "learning", "certified", "fluent"), "skill"),
    (("project", "building", "working on", "repo", "launch"), "project"),
    (("wife", "husband", "son", "daughter", "friend", "manager", "colleague", "dog", "cat", "pet"), "relationship"),
    (("every", "always", "usually", "tends to", "habit", "routine", "each week"), "behavior"),
    (("yesterday", "last week", "last month", "attended", "visited", "happened"), "event"),
)

#: Predicates a person can hold exactly one value for at a time. Two memories of
#: the same type that share one of these and disagree on the value contradict
#: each other, even with no change verb present — "User lives in Chennai" versus
#: "User lives in Bangalore".
_GLOBALLY_UNIQUE_PREDICATES: tuple[tuple[str, ...], ...] = (
    ("live", "lives", "living", "lived", "based", "reside", "resides", "located", "hometown"),
    ("work", "works", "working", "employed", "employer", "job", "role", "title", "position"),
    ("studies", "studying", "studied", "degree", "major", "university", "college"),
    ("drives", "drive", "vehicle"),
    ("timezone", "shift"),
)

#: Predicates that are single-valued *per slot* rather than globally. Someone has
#: one default browser and one default editor, but "prefers" alone proves
#: nothing: preferring aisle seats does not contradict preferring dark mode.
#:
#: A conflict here therefore requires the two memories to name the same slot —
#: at least one shared word outside the predicate itself. Without that rule the
#: heuristic retires an unrelated preference every time a new one arrives, which
#: is the most damaging mistake this system can make.
_SLOT_SCOPED_PREDICATES: tuple[tuple[str, ...], ...] = (
    ("prefers", "prefer", "preferred", "favourite", "favorite", "default"),
    ("uses", "use", "using", "runs", "stack", "framework", "editor", "ide", "browser", "cloud", "provider"),
    ("database", "db", "datastore", "warehouse"),
    ("company", "team", "budget", "version", "goal"),
)

#: Words that open a message carrying no durable fact.
_GREETING_OPENERS = frozenset(
    ["hi", "hey", "hello", "yo", "hiya", "greetings", "thanks", "thank", "ok", "okay", "sure", "bye", "goodbye", "cheers", "morning", "afternoon", "evening", "night", "good", "welcome", "yes", "no", "nope", "yeah", "yep", "sorry"]
)

_SENSITIVE = re.compile(
    r"\b(?:password|passwd|api[_ -]?key|secret|token|otp|cvv|ssn|aadhaar|passport"
    r"|credit card|card number|bank account|diagnos|prescription|salary)\b",
    re.IGNORECASE,
)
_SENSITIVE_PATTERNS = (
    re.compile(r"\b\d{12,19}\b"),  # card / account numbers
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),  # email addresses
)


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall((text or "").lower()) if t not in _STOPWORDS and len(t) > 1}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _containment(inner: set[str], outer: set[str]) -> float:
    """Fraction of ``inner`` covered by ``outer`` — detects a richer restatement,
    which Jaccard alone misses because the longer text dilutes the score."""
    if not inner:
        return 0.0
    return len(inner & outer) / len(inner)


def _view_id(item: dict[str, Any]) -> str:
    """Id of an existing memory as the decision payload renders it.

    `_memory_view` uses short keys — the payload is billed by the token and ten
    candidates carry ten copies of every key name. The longer spellings are
    still accepted because the *candidate* half of the payload uses them.
    """
    return str(item.get("id") or item.get("memoryId") or "")


def _view_text(item: dict[str, Any]) -> str:
    return str(item.get("text") or item.get("content") or "")


def _view_type(item: dict[str, Any]) -> str | None:
    return item.get("type") or item.get("memoryType")


class FakeLLMProvider(LLMProvider):
    def __init__(self, model_name: str = "fake-llm") -> None:
        self._model_name = model_name
        self._scripted: list[dict[str, Any]] = []
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        #: Every (system, user, schema) triple seen, for test assertions.
        self.calls: list[dict[str, Any]] = []

    @property
    def model_name(self) -> str:
        return f"fake:{self._model_name}"

    # --- scripting --------------------------------------------------------- #

    def push(self, response: dict[str, Any]) -> None:
        """Queue one exact response, consumed FIFO."""
        self._scripted.append(response)

    def push_many(self, responses: list[dict[str, Any]]) -> None:
        self._scripted.extend(responses)

    def register(self, task: str, handler: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._handlers[task] = handler

    def reset(self) -> None:
        self._scripted.clear()
        self._handlers.clear()
        self.calls.clear()

    # --- API ---------------------------------------------------------------- #

    async def generate_json(
        self,
        *,
        system: str,
        user: str,
        response_model: type[T],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult[T]:
        started = time.perf_counter()
        labels = {"provider": "fake", "operation": current_operation()}
        metrics.increment(LLM_CALLS, labels=labels)

        try:
            payload = json.loads(user)
        except json.JSONDecodeError:
            payload = {"task": "unknown", "raw": user}

        self.calls.append(
            {"system": system, "user": user, "payload": payload, "schema": response_model.__name__}
        )

        if self._scripted:
            response = self._scripted.pop(0)
        else:
            task = str(payload.get("task", ""))
            handler = self._handlers.get(task)
            response = handler(payload) if handler else self._heuristic(task, payload)

        data = validate_response(response, response_model)
        latency_ms = (time.perf_counter() - started) * 1000.0
        metrics.observe(LLM_LATENCY, latency_ms, labels)

        # Approximated from length so the offline path exercises the same
        # token-reporting code as a real provider.
        raw = json.dumps(response)
        usage = {
            "prompt_tokens": max(1, (len(system) + len(user)) // 4),
            "completion_tokens": max(1, len(raw) // 4),
        }
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        metrics.increment(LLM_PROMPT_TOKENS, float(usage["prompt_tokens"]), labels)
        metrics.increment(LLM_COMPLETION_TOKENS, float(usage["completion_tokens"]), labels)
        metrics.increment(LLM_TOKENS, float(usage["total_tokens"]), labels)

        return LLMResult(
            data=data,
            model=self.model_name,
            latency_ms=latency_ms,
            usage=usage,
            raw=raw,
        )

    # --- heuristics ---------------------------------------------------------- #

    def _heuristic(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
        if task == "extract":
            return self._extract(payload)
        if task == "decide":
            return self._decide(payload)
        if task == "synthesize":
            return self._synthesize(payload)
        raise LLMResponseError(f"FakeLLMProvider has no heuristic for task {task!r}")

    # -- extraction -- #

    @staticmethod
    def _classify(text: str) -> str:
        lowered = text.lower()
        for keywords, memory_type in _TYPE_KEYWORDS:
            if any(keyword in lowered for keyword in keywords):
                return memory_type
        return "fact"

    @staticmethod
    def _to_third_person(text: str) -> str:
        """Rewrite first person into the third person the store uses.

        Crude but deterministic. A real model does this properly; the point here
        is that stored content reads consistently regardless of provider.
        """
        result = text.strip()
        substitutions = (
            (r"^i'm\b", "User is"),
            (r"^i am\b", "User is"),
            (r"^i've\b", "User has"),
            (r"^i have\b", "User has"),
            (r"^i\b", "User"),
            (r"^my\b", "User's"),
            (r"\bi'm\b", "user is"),
            (r"\bi am\b", "user is"),
            (r"\bi\b", "user"),
            (r"\bmy\b", "user's"),
            (r"\bme\b", "user"),
            (r"\bmine\b", "user's"),
        )
        for pattern, replacement in substitutions:
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
        if not result.lower().startswith("user"):
            result = f"User: {result}" if len(result.split()) < 3 else result
        return result[0].upper() + result[1:] if result else result

    def _extract(self, payload: dict[str, Any]) -> dict[str, Any]:
        message = str(payload.get("message", "")).strip()
        if not message:
            return {"hasMemory": False, "memories": [], "reason": "empty message"}

        lowered = message.lower()
        message_tokens = _TOKEN.findall(lowered)

        # Pure chit-chat and questions carry nothing worth keeping.
        #
        # A greeting is recognised by opening with a greeting word, staying
        # short, and containing no first-person reference — rather than by exact
        # match, which would miss "hi there" and "hello, how are you". The
        # first-person guard is what keeps "hi, I moved to Bangalore" from being
        # discarded as small talk.
        content_tokens = _tokens(message)
        if (
            message_tokens
            and message_tokens[0] in _GREETING_OPENERS
            and len(content_tokens) <= 3
            and not re.search(r"\b(i|i'm|my|we|our|me)\b", lowered)
        ):
            return {"hasMemory": False, "memories": [], "reason": "greeting, no durable fact"}
        if message.endswith("?") and not any(
            marker in lowered for marker in ("i ", "my ", "we ")
        ):
            return {"hasMemory": False, "memories": [], "reason": "question, not a statement"}

        # One memory per sentence keeps distinct facts separately supersedable.
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\s+and also\s+", message) if s.strip()]
        memories: list[dict[str, Any]] = []
        for sentence in sentences[:8]:
            if len(_tokens(sentence)) < 2:
                continue
            lowered_sentence = sentence.lower()
            sensitive = bool(_SENSITIVE.search(sentence)) or any(
                pattern.search(sentence) for pattern in _SENSITIVE_PATTERNS
            )
            if any(marker in lowered_sentence for marker in ("used to", "previously", "no longer")):
                temporal = "historical"
            elif any(marker in lowered_sentence for marker in ("will ", "next month", "next year", "planning to")):
                temporal = "future"
            elif any(marker in lowered_sentence for marker in ("today", "this week", "temporarily", "for now")):
                temporal = "temporary"
            elif any(marker in lowered_sentence for marker in ("born", "birthday", "always been")):
                temporal = "permanent"
            else:
                temporal = "current"

            memory_type = self._classify(sentence)
            # Operational facts outrank tastes; that ordering drives retrieval.
            importance = 0.75 if memory_type in ("profile", "constraint", "goal", "project") else 0.45
            if memory_type == "preference":
                importance = 0.35
            memories.append(
                {
                    "content": self._to_third_person(sentence),
                    "memoryType": memory_type,
                    "importance": importance,
                    "confidence": 0.95,
                    "explicit": True,
                    "temporalNature": temporal,
                    "sensitive": sensitive,
                }
            )

        return {
            "hasMemory": bool(memories),
            "memories": memories,
            "reason": "heuristic extraction" if memories else "no durable fact found",
        }

    # -- decision -- #

    @staticmethod
    def _frame_targets(
        candidate_text: str, existing: list[dict[str, Any]], candidate_type: Any
    ) -> list[str]:
        """Existing memories describing the attribute this message changes.

        Restricted to memories of the same type, so a message about moving house
        cannot retire a memory about the user's employer just because both are
        `profile` facts and one mentions a city.
        """
        targets: list[str] = []
        for markers, attributes in _CHANGE_FRAMES:
            if not any(marker in candidate_text for marker in markers):
                continue
            for item in existing:
                if candidate_type and _view_type(item) != candidate_type:
                    continue
                item_tokens = _tokens(_view_text(item))
                if item_tokens & set(attributes):
                    memory_id = _view_id(item)
                    if memory_id not in targets:
                        targets.append(memory_id)
        return targets

    @staticmethod
    def _predicate_conflicts(
        candidate_tokens: set[str], existing: list[dict[str, Any]], candidate_type: Any
    ) -> list[str]:
        """Memories asserting a different value for the same single-valued predicate.

        Catches contradictions with no change verb in them, which a marker-based
        rule cannot see: "User lives in Chennai" and "User lives in Bangalore"
        share the predicate `lives` and disagree on everything else.
        """
        targets: list[str] = []
        groups = [(p, True) for p in _GLOBALLY_UNIQUE_PREDICATES]
        groups += [(p, False) for p in _SLOT_SCOPED_PREDICATES]

        for predicate, globally_unique in groups:
            predicate_set = set(predicate)
            shared = candidate_tokens & predicate_set
            if not shared:
                continue

            candidate_values = candidate_tokens - predicate_set
            for item in existing:
                if candidate_type and _view_type(item) != candidate_type:
                    continue
                item_tokens = _tokens(_view_text(item))
                if not (item_tokens & shared):
                    continue

                item_values = item_tokens - predicate_set
                if not candidate_values or not item_values:
                    continue

                # Slot-scoped predicates need the two memories to be about the
                # same thing before their values can disagree.
                if not globally_unique and not (candidate_values & item_values):
                    continue

                # Enrichment is not contradiction. If the stored memory's values
                # are all present in the new one, the new text restates it with
                # more detail — "PostgreSQL" versus "PostgreSQL 16 on RDS" — and
                # belongs to merge. A real conflict means the old memory asserts
                # something the new one drops: "MongoDB" versus "PostgreSQL".
                if _containment(item_values, candidate_values) >= 0.8:
                    continue

                # Values that are nearly identical are a duplicate for the merge
                # branch to handle, not a contradiction. The bar is generous
                # because the two sides usually share their qualifiers
                # ("production database") and differ only in the value itself.
                if _jaccard(candidate_values, item_values) < 0.7:
                    memory_id = _view_id(item)
                    if memory_id not in targets:
                        targets.append(memory_id)

        return targets

    def _decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        candidate = payload.get("candidate") or {}
        content = str(candidate.get("content", "")).strip()
        existing: list[dict[str, Any]] = payload.get("existingMemories") or []

        base = {
            "content": content,
            "memoryType": candidate.get("memoryType", "other"),
            "confidence": candidate.get("confidence", 0.9),
            "importance": candidate.get("importance", 0.5),
            "temporalNature": candidate.get("temporalNature", "current"),
        }
        if not existing:
            return {"action": "ADD", "reason": "no related memory exists", "memory": base, "targetMemoryIds": []}

        candidate_tokens = _tokens(content)
        lowered = content.lower()

        scored = [
            (
                _jaccard(candidate_tokens, _tokens(_view_text(item))),
                _containment(_tokens(_view_text(item)), candidate_tokens),
                item,
            )
            for item in existing
        ]
        scored.sort(key=lambda row: row[0], reverse=True)
        overlap, containment, best = scored[0]
        best_id = _view_id(best)
        best_content = _view_text(best)

        # Same fact, same words.
        if overlap >= 0.92:
            return {
                "action": "NOOP",
                "reason": "duplicate of an existing active memory",
                "memory": None,
                "targetMemoryIds": [best_id],
            }

        has_change_marker = any(marker in lowered for marker in _CHANGE_MARKERS)

        # A newer statement that changes a specific attribute of the same kind of
        # fact. Frame matching first (works at zero lexical overlap), then a
        # similarity fallback for change markers outside the frame table.
        supersede_targets = self._frame_targets(lowered, existing, candidate.get("memoryType"))
        supersede_targets += [
            memory_id
            for memory_id in self._predicate_conflicts(
                candidate_tokens, existing, candidate.get("memoryType")
            )
            if memory_id not in supersede_targets
        ]
        if not supersede_targets and has_change_marker:
            supersede_targets = [
                _view_id(item)
                for score, _, item in scored
                if score >= 0.25 and _view_type(item) == candidate.get("memoryType")
            ]

        if supersede_targets:
            return {
                "action": "SUPERSEDE",
                "reason": "new statement replaces an outdated fact about the same attribute",
                "memory": base,
                "targetMemoryIds": supersede_targets,
            }

        # The new text says everything an existing one did, plus more. Ranked by
        # containment rather than Jaccard: a longer restatement dilutes Jaccard
        # even when it fully subsumes the original, so the best enrichment
        # candidate is often not the best Jaccard match.
        enrichment = max(scored, key=lambda row: row[1])
        _, enrichment_containment, enrichment_item = enrichment
        if enrichment_containment >= 0.6 and len(candidate_tokens) > len(
            _tokens(_view_text(enrichment_item))
        ):
            return {
                "action": "MERGE",
                "reason": "new memory is a richer version of an existing one",
                "memory": {**base, "content": content},
                "targetMemoryIds": [_view_id(enrichment_item)],
            }

        # Substantial overlap without new information. The bar sits at 0.45
        # because short third-person facts are mostly content words: "User likes
        # black coffee" and "User prefers black coffee" share two of four and are
        # plainly the same fact, while "User lives in Bangalore" and "User works
        # in Bangalore" share one of three and are plainly not.
        if overlap >= 0.45:
            richer = content if len(content) >= len(best_content) else best_content
            return {
                "action": "MERGE",
                "reason": "semantic duplicate consolidated into one canonical memory",
                "memory": {**base, "content": richer},
                "targetMemoryIds": [best_id],
            }

        return {"action": "ADD", "reason": "distinct new fact", "memory": base, "targetMemoryIds": []}

    # -- synthesis -- #

    def _synthesize(self, payload: dict[str, Any]) -> dict[str, Any]:
        memories: list[dict[str, Any]] = payload.get("memories") or []
        patterns: list[dict[str, Any]] = payload.get("existingPatterns") or []

        if len(memories) < 3:
            return {"action": "NOOP", "reason": "not enough evidence for a pattern", "pattern": None}

        source_ids = [_view_id(item) for item in memories]

        # Idempotency: an existing pattern resting on mostly the same evidence
        # means this cluster has already been synthesised.
        for pattern in patterns:
            existing_sources = set(pattern.get("sourceMemoryIds") or [])
            if not existing_sources:
                continue
            overlap = len(existing_sources & set(source_ids)) / len(set(source_ids))
            if overlap >= 0.6:
                return {
                    "action": "NOOP",
                    "reason": f"pattern {pattern.get('memoryId')} already covers this evidence",
                    "pattern": None,
                }

        counts: Counter[str] = Counter()
        for item in memories:
            counts.update(_tokens(_view_text(item)))

        ranked = [word for word, _ in counts.most_common(10)]
        recurring = [word for word, count in counts.most_common(10) if count >= 2]

        # Evidence for a pattern is either a repeated theme, or simply enough
        # memories that the clusterer already judged mutually similar. Insisting
        # on repeated vocabulary would reject real patterns whose members share a
        # topic but no words — five tools of the same kind, named differently.
        if not recurring and len(memories) < 4:
            return {
                "action": "NOOP",
                "reason": "no recurring theme and too little evidence",
                "pattern": None,
            }

        # Recurring words first, then the distinctive ones. Recurring words alone
        # tend to be the generic verbs the sentences share ("uses", "prefers"),
        # which makes for a vacuous pattern *and* one that no longer resembles
        # the pattern it duplicates — defeating the semantic duplicate check.
        themes = recurring + [word for word in ranked if word not in recurring]

        return {
            "action": "SYNTHESIZE",
            "reason": "recurring theme across multiple memories",
            "pattern": {
                "content": (
                    "User consistently shows a pattern involving "
                    + ", ".join(themes[:6])
                    + f" (observed across {len(memories)} memories)."
                ),
                "memoryType": "pattern",
                "confidence": min(0.95, 0.55 + 0.05 * len(memories)),
                "importance": min(0.9, 0.5 + 0.04 * len(memories)),
                "sourceMemoryIds": source_ids,
            },
        }
