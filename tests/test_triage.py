"""The gate in front of extraction.

Its whole value is being *safe*, not being clever. A false pass costs a fraction
of a cent; a false skip loses a memory forever, silently, and the user finds out
weeks later when the bot has forgotten their allergy.

So the second half of this file matters more than the first. Every entry in
`MUST_REACH_THE_MODEL` is something a plausible heuristic would eat.
"""

from __future__ import annotations

import pytest

from app.memory.triage import skip_reason

# --------------------------------------------------------------------------- #
# Skipped — structurally incapable of stating a fact about the user
# --------------------------------------------------------------------------- #

FILLER = [
    "hey",
    "Hey!",
    "hi",
    "hello",
    "wassup",
    "sup",
    "yo",
    "Good morning",
    "good night",
    "ok",
    "okay",
    "Okay!!",
    "kk",
    "sure",
    "yep",
    "cool",
    "nice",
    "perfect",
    "got it",
    "thanks",
    "Thank you",
    "thanks a lot",
    "thank you so much",
    "no problem",
    "np",
    "bye",
    "see you",
    "take care",
    "lol",
    "haha",
    "hmm",
    "wow",
    "how are you",
    "How's it going?",
    "hello there",
    "brb",
    "one sec",
    "👍",
    "!!!",
    "   ",
]


@pytest.mark.parametrize("message", FILLER)
def test_filler_never_reaches_the_model(message):
    assert skip_reason(message) is not None, f"{message!r} would cost ~660 tokens"


QUESTIONS = [
    "what time do you close?",
    "Do you deliver to Adyar?",
    "how much is the thali?",
    "Is the paneer spicy?",
    "can you repeat that?",
    "When does the offer end?",
]


@pytest.mark.parametrize("message", QUESTIONS)
def test_questions_that_assert_nothing_are_skipped(message):
    assert skip_reason(message) == "question, not a statement"


# --------------------------------------------------------------------------- #
# Must reach the model — the expensive half of the contract
# --------------------------------------------------------------------------- #

MUST_REACH_THE_MODEL = [
    # Short, and every one of them is a real memory. A length heuristic eats
    # all of these, which is why there isn't one.
    "vegetarian",
    "no onions",
    "no beef",
    "not spicy",
    "allergic to nuts",
    "peanut allergy",
    "without sugar",
    "veg only",
    # Statements that open with filler.
    "hi, I'm vegetarian",
    "hey, my number is 98765 43210",
    "ok, deliver to 22 Anna Nagar",
    "thanks — I'll take the veg thali from now on",
    "sure, but no onions please",
    # Questions that also state something.
    "what should I order? I'm allergic to peanuts",
    "can you make it without onions?",
    "do you have anything I can eat? I don't eat beef",
    # Ordinary memories.
    "My name is Priya Raman",
    "For breakfast I always get the masala dosa",
    "I've moved to Anna Nagar",
    "I love your gulab jamun",
    "Deliver to 14 Habibullah Road, T. Nagar",
]


@pytest.mark.parametrize("message", MUST_REACH_THE_MODEL)
def test_anything_that_might_be_a_memory_reaches_the_model(message):
    assert skip_reason(message) is None, (
        f"{message!r} was skipped — that memory is now lost, silently"
    )


# --------------------------------------------------------------------------- #
# The rules behind those cases
# --------------------------------------------------------------------------- #


def test_filler_is_matched_whole_never_as_a_substring():
    """"hi" is filler. "hi, I'm vegetarian" opens with it and is a memory."""
    assert skip_reason("hi") is not None
    assert skip_reason("hi I am vegetarian") is None


def test_a_first_person_statement_always_wins():
    """First person means the user may be describing themselves, which takes the
    decision out of the gate's hands whatever else the message looks like."""
    assert skip_reason("ok") is not None
    assert skip_reason("ok I will have the thali") is None


def test_negations_always_win():
    """Constraints are the shortest memories there are, and the most costly to
    lose."""
    for message in ("no onions", "not spicy", "never send peanuts", "without ice"):
        assert skip_reason(message) is None, message


def test_a_question_containing_a_statement_is_not_skipped():
    assert skip_reason("what time do you close?") is not None
    assert skip_reason("what time do you close? I finish work at 6") is None


def test_a_question_mark_is_required_to_call_something_a_question():
    """Deliberately conservative. Without the mark, "when the delivery arrives
    leave it at the door" is an instruction worth keeping, not a question — and
    the gate cannot tell those apart, so it declines to try."""
    assert skip_reason("what time do you close?") is not None
    assert skip_reason("how do you make it") is None
    assert skip_reason("when the delivery arrives leave it at the door") is None


def test_the_reason_is_specific_enough_to_show_a_user():
    """It lands on the audit row, so "nothing was remembered" stays answerable."""
    assert skip_reason("hey") == "greeting or acknowledgement"
    assert skip_reason("what time is it?") == "question, not a statement"
    assert skip_reason("") == "empty message"
    assert skip_reason("👍") == "no words"


def test_unicode_and_case_are_folded():
    for message in ("HEY", "Hey", "ｈｅｙ", "  hey  ", "Hey!!!"):
        assert skip_reason(message) is not None, message
