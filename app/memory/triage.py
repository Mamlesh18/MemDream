"""The cheapest decision in the system: whether to ask the model at all.

A `"hey"` costs one token to say and ~660 to reason about, because the prompt is
a fixed cost paid per call regardless of message length. In a chat product a
third to a half of turns are greetings, acknowledgements and questions — none of
which can contain a durable fact — and every one of them was being sent to an
LLM.

This module is a deterministic gate in front of extraction. It returns a reason
when a message provably cannot contain a memory, and `None` for everything else.

## Why it is a lexicon and not a heuristic

The two errors are not symmetric:

    false skip   a real memory is lost, forever, silently
    false pass   you spend $0.0001

So the gate only fires when it is *certain*, which rules out every clever
shortcut. Length in particular is a trap — `"vegetarian"` is ten characters,
`"no onions"` is nine, `"allergic to nuts"` is sixteen, and those are exactly
the facts a memory layer exists to keep. A short message is not an empty one.

What is left is a closed set of things that are structurally incapable of
stating a fact about the user: fixed social formulae, and questions that assert
nothing. Anything outside it goes to the model, which is the expensive but
correct default.
"""

from __future__ import annotations

import re
import unicodedata

#: Fixed social formulae. Matched against the whole normalised message, never a
#: substring — "hi, I'm vegetarian" must not look like "hi".
_FILLER = frozenset(
    ["hi", "hello", "hey", "heya", "hiya", "yo", "sup", "wassup", "whatsup", "wazzup", "howdy", "hola", "namaste", "good", "morning", "good", "afternoon", "good", "evening", "good", "night", "goodnight", "morning", "ok", "okay", "okey", "kk", "k", "sure", "yep", "yup", "yeah", "yea", "yes", "no", "nope", "nah", "fine", "cool", "nice", "great", "awesome", "perfect", "lovely", "excellent", "good", "got", "it", "gotcha", "understood", "right", "alright", "noted", "done", "same", "ditto", "true", "thanks", "thank", "you", "thankyou", "thx", "tysm", "ty", "cheers", "appreciated", "much", "appreciated", "please", "sorry", "oops", "my", "bad", "np", "no", "problem", "no", "worries", "welcome", "bye", "goodbye", "see", "you", "see", "ya", "cya", "later", "ttyl", "take", "care", "good", "bye", "lol", "lmao", "haha", "hahaha", "hehe", "hmm", "hmmm", "huh", "oh", "ah", "aha", "wow", "omg", "brb", "one", "sec", "hold", "on", "wait", "a", "sec", "just", "a", "sec", "test", "testing", "ping", "hello", "there", "there"]
)

#: Multi-word formulae the split above cannot express.
_FILLER_PHRASES = frozenset(
    {
        "good morning", "good afternoon", "good evening", "good night",
        "thank you", "thanks a lot", "thank you so much", "much appreciated",
        "no problem", "no worries", "got it", "see you", "see ya", "take care",
        "one sec", "hold on", "wait a sec", "just a sec", "my bad",
        "how are you", "how are you doing", "how's it going", "hows it going",
        "what's up", "whats up", "you there", "are you there", "hello there",
    }
)

#: A message opening with one of these and asserting nothing is a question.
_QUESTION_OPENERS = frozenset(
    ["what", "when", "where", "why", "who", "whom", "whose", "which", "how", "is", "are", "was", "were", "do", "does", "did", "can", "could", "will", "would", "should", "shall", "may", "might", "have", "has", "had", "am"]
)

#: First-person markers. Their presence means the user may be stating something
#: about themselves, which takes the message out of the gate's hands entirely.
_FIRST_PERSON = re.compile(
    r"\b(i|i'm|im|i've|ive|i'd|id|i'll|ill|me|my|mine|myself|we|our|us)\b"
)

#: Negated preferences and constraints — "no onions", "not spicy", "without
#: sugar". Short, factual, and easy for a naive gate to eat.
_CONSTRAINT = re.compile(
    r"\b(no|not|never|without|avoid|allerg\w*|cannot|can't|dont|don't|doesn't)\b"
)

_PUNCTUATION = re.compile(r"[^\w\s']+")
_WHITESPACE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text or "").casefold().strip()
    folded = _PUNCTUATION.sub(" ", folded)
    return _WHITESPACE.sub(" ", folded).strip()


def skip_reason(text: str) -> str | None:
    """Why this message cannot contain a memory, or None to send it to the model.

    The returned string is recorded on the audit row, so "nothing was
    remembered" stays answerable without reading logs.
    """
    if not text or not text.strip():
        return "empty message"

    normalised = _normalise(text)
    if not normalised:
        # Punctuation or emoji only.
        return "no words"

    words = normalised.split()

    # Whole-message filler first. These are checked before the escapes below
    # because a few of them contain words that look like constraints — "no
    # problem", "no worries" — and every word in the lexicon is meaningless on
    # its own, so a whole message made of them cannot be a memory.
    if normalised in _FILLER_PHRASES or all(word in _FILLER for word in words):
        return "greeting or acknowledgement"

    # Past this point, anything that might state something about the user goes
    # to the model whatever else it looks like: "hi, I'm vegetarian" opens with
    # filler and is a memory, "no onions" is a negation and is a memory.
    if _FIRST_PERSON.search(normalised) or _CONSTRAINT.search(normalised):
        return None

    # A question with no first-person statement asserts nothing about the user.
    # "what time do you close?" is not a memory; "what should I order, I'm
    # vegetarian?" never reaches here because of the check above.
    if words[0] in _QUESTION_OPENERS and text.rstrip().endswith("?"):
        return "question, not a statement"

    return None
