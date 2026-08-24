"""The prompts have a token budget, and it is enforced here.

Prompt bloat is invisible in review and expensive in production: every write
pays extraction plus a decision, so a paragraph added "just to be safe" is a
paragraph charged on every message from every user forever. The originals had
drifted to ~3,800 tokens across five prompts, most of it explaining *why* rules
existed to a reader who was not there.

Cutting the justifications and keeping every rule took that to ~1,400. The
ceilings below are what stops it drifting back. If a change genuinely needs more
room, raise the number here deliberately rather than discovering the cost in a
bill.

Approximating tokens as characters÷4 is crude but stable across models, and
being wrong by 10% does not matter for a guard rail.
"""

from __future__ import annotations

import pytest

from app.dream import prompts

RESTAURANT = (
    "A restaurant ordering assistant. Remember contact and delivery details, "
    "dietary needs, dislikes, and usual orders per meal."
)

#: Per-prompt ceilings, in approximate tokens, *excluding* the domain line.
#: `decision` is the outlier on purpose: it is the only prompt carrying both the
#: supersede and merge rule sets, because it decides between them in one call.
#: Staged mode splits it into `supersede` and `merge`, which is why those two sit
#: comfortably inside 300.
BUDGET = {
    "extraction": 245,
    "decision": 320,
    "supersede": 210,
    "merge": 230,
    "synthesis": 310,
}

#: The output shape sent alongside each prompt, hand-written on the model rather
#: than generated from it. Counted separately because it is the other half of
#: the fixed cost, and it used to be the *larger* half.
SHAPE_BUDGET = {"ExtractionResult": 145, "DreamDecision": 120, "SynthesisDecision": 60}

BUILDERS = {
    "extraction": prompts.extraction_system,
    "decision": prompts.decision_system,
    "supersede": prompts.supersede_system,
    "merge": prompts.merge_system,
    "synthesis": prompts.synthesis_system,
}


def approx_tokens(text: str) -> int:
    return len(text) // 4


@pytest.mark.parametrize("name", sorted(BUDGET))
def test_each_prompt_stays_within_budget(name):
    size = approx_tokens(BUILDERS[name]())
    assert size <= BUDGET[name], (
        f"{name} prompt is ~{size} tokens, budget is {BUDGET[name]}. "
        "Cut something, or raise the budget deliberately."
    )


@pytest.mark.parametrize("name", sorted(SHAPE_BUDGET))
def test_each_output_shape_stays_within_budget(name):
    from app.models import dream
    from app.providers.llm.base import schema_hint

    model = getattr(dream, name)
    size = approx_tokens(schema_hint(model))
    assert size <= SHAPE_BUDGET[name], (
        f"{name} shape is ~{size} tokens, budget is {SHAPE_BUDGET[name]}"
    )


@pytest.mark.parametrize("name", sorted(SHAPE_BUDGET))
def test_the_hand_written_shape_is_actually_being_used(name):
    """If `prompt_shape` is dropped from a model, `schema_hint` silently falls
    back to generation and the call quietly doubles in size."""
    from app.models import dream

    assert getattr(dream, name).prompt_shape.strip(), f"{name} has no prompt_shape"


def test_the_fixed_cost_of_a_write_stays_under_800():
    """Prompt plus shape for extraction and one decision — everything billed on
    a remembered message that is *not* the message or its candidates.

    800 rather than a rounder number because that is what the rules cost once
    the prose around them is gone. Going lower means deleting a rule, and every
    one of them is in `test_*_keeps_its_hard_won_rules` because it was added to
    fix something that actually happened — three of them were re-added after a
    live run against the real model showed the shorter prompt breaking."""
    from app.models.dream import DreamDecision, ExtractionResult
    from app.providers.llm.base import schema_hint

    fixed = (
        approx_tokens(prompts.extraction_system())
        + approx_tokens(schema_hint(ExtractionResult))
        + approx_tokens(prompts.decision_system())
        + approx_tokens(schema_hint(DreamDecision))
    )
    assert fixed <= 800, f"a write carries ~{fixed} fixed tokens"


def test_a_candidate_memory_costs_under_60_tokens():
    """The dominant cost of a decision: ten candidates at the old 88 tokens was
    880 tokens of payload against a 380-token prompt."""
    from app.dream.prompts import _dumps, _memory_view
    from app.models.memory import Memory

    memory = Memory(
        user_id="u",
        content="User usually orders the veg thali for lunch and does not like onions.",
        memory_type="preference",
    )
    size = approx_tokens(_dumps({"v": _memory_view(memory)}))
    assert size <= 60, f"a candidate costs ~{size} tokens"


def test_the_domain_line_is_cheap():
    """It has to be, or nobody will use it. One sentence, injected into three
    prompts, must not undo the savings."""
    for name, build in BUILDERS.items():
        overhead = approx_tokens(build(RESTAURANT)) - approx_tokens(build())
        assert overhead <= 45, f"{name} pays {overhead} tokens for the domain line"


# --------------------------------------------------------------------------- #
# The rules that were cut down to, not cut out
# --------------------------------------------------------------------------- #
#
# Each of these was added to fix a specific misbehaviour. Shortening the prompts
# meant deleting the prose around them, never the rule — and a future edit that
# trims "one more sentence" is exactly how one would disappear.


@pytest.mark.parametrize(
    ("fragment", "why"),
    [
        ("Likes and dislikes both count", "both directions must be extracted"),
        ("cannot change by nature", "permanent must be defined, not assumed"),
        ("`permanent` protects", "and its cost has to be stated"),
        ("never drops one", "importance ranks, it does not filter"),
        ("Never infer past what was said", "no embellishment"),
        ("Split a message", "one fact per memory"),
    ],
)
def test_extraction_keeps_its_hard_won_rules(fragment, why):
    assert fragment in prompts.extraction_system(), why


@pytest.mark.parametrize(
    ("fragment", "why"),
    [
        ("added detail is\nMERGE", "additive detail is not a contradiction"),
        ("SUPERSEDE when the new fact makes an old one untrue",
         "a rules block that only forbids stops superseding at all"),
        ("different subjects", "two dishes never conflict"),
        ("rewrites history", "historical facts survive a change"),
        ("Permanent facts are never replaced", "birthplace does not move"),
        ("ids exactly as given", "the model must not invent ids"),
        ("choose ADD", "prefer adding over wrongly retiring"),
    ],
)
def test_decision_keeps_its_hard_won_rules(fragment, why):
    assert fragment in prompts.decision_system(), why


def test_sensitive_does_not_swallow_dietary_needs():
    """The trap this nearly shipped with: an allergy read as a health condition,
    flagged sensitive, and rejected outright by the default policy — a
    restaurant bot silently forgetting a peanut allergy."""
    from app.models.dream import ExtractionResult

    prompt = prompts.extraction_system()
    shape = ExtractionResult.prompt_shape

    # Stated as what *is* sensitive plus what is ordinary. An earlier phrasing —
    # "never set it for allergies" — was read by the model as "reject
    # allergies", and it refused to extract a peanut allergy at all.
    assert "payment details, passwords and\ngovernment ids" in prompt
    assert "allergies and dietary needs are\nordinary memories" in prompt
    assert "discards the memory" in prompt
    assert "health condition" not in prompt and "health" not in shape


def test_synthesis_still_demands_traceable_sources():
    text = prompts.synthesis_system()
    assert "sourceMemoryIds" in text
    assert "without sources is rejected" in text


# --------------------------------------------------------------------------- #
# The domain line
# --------------------------------------------------------------------------- #


def test_the_domain_line_reaches_the_prompts_that_use_it():
    for name in ("extraction", "decision", "supersede", "merge", "synthesis"):
        assert RESTAURANT in BUILDERS[name](RESTAURANT), f"{name} ignores the domain"


def test_no_domain_leaves_no_trace():
    """An empty setting must not leave a dangling "Context:" header."""
    for build in BUILDERS.values():
        assert "Context:" not in build()
        assert "Context:" not in build("   ")


def test_the_default_is_general_purpose():
    """MemDream is not a restaurant framework. The domain is opt-in."""
    from app.core.config import Settings

    assert Settings(env="test", identity_salt="t", domain="").domain == ""
    assert prompts.DEFAULT_DOMAIN == ""
