"""LLM provider tests: JSON extraction, validation, and the repair round-trip.

The repair path is the least-exercised code in the system and the most likely to
be reached in production — it fires the first time a model returns something
that does not satisfy the schema. It gets a direct test here because the fake
provider never triggers it.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import LLMResponseError
from app.models.dream import DreamDecision, ExtractionResult
from app.providers.llm.base import extract_json_object, schema_hint, validate_response

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# JSON extraction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"action": "ADD"}', {"action": "ADD"}),
        ('```json\n{"action": "ADD"}\n```', {"action": "ADD"}),
        ('```\n{"action": "ADD"}\n```', {"action": "ADD"}),
        ('Here is my answer:\n{"action": "ADD"}', {"action": "ADD"}),
        ('  \n {"action": "ADD"}  \n ', {"action": "ADD"}),
        ('{"a": {"b": {"c": 1}}}', {"a": {"b": {"c": 1}}}),
    ],
)
async def test_extract_json_handles_what_models_actually_return(raw, expected):
    assert extract_json_object(raw) == expected


async def test_extract_json_is_string_aware():
    """A brace inside a string value must not terminate the scan."""
    raw = 'Sure!\n{"reason": "the user said {this} and }that{", "action": "ADD"}'
    assert extract_json_object(raw)["action"] == "ADD"


async def test_extract_json_handles_escaped_quotes():
    raw = r'{"reason": "he said \"hello\"", "action": "NOOP"}'
    assert extract_json_object(raw)["reason"] == 'he said "hello"'


@pytest.mark.parametrize("raw", ["", "   ", "no json here at all", '{"unterminated": '])
async def test_extract_json_rejects_unusable_responses(raw):
    with pytest.raises(LLMResponseError):
        extract_json_object(raw)


async def test_extract_json_rejects_a_bare_array():
    with pytest.raises(LLMResponseError):
        extract_json_object("[1, 2, 3]")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


async def test_validation_rejects_an_out_of_schema_action():
    with pytest.raises(LLMResponseError):
        validate_response({"action": "DELETE_EVERYTHING"}, DreamDecision)


async def test_validation_applies_defaults():
    decision = validate_response({"action": "ADD"}, DreamDecision)
    assert decision.target_memory_ids == []
    assert decision.memory is None


async def test_validation_coerces_a_single_id_to_a_list():
    decision = validate_response(
        {"action": "SUPERSEDE", "targetMemoryIds": "mem_1"}, DreamDecision
    )
    assert decision.target_memory_ids == ["mem_1"]


async def test_validation_parses_loose_dates():
    decision = validate_response(
        {"action": "SUPERSEDE", "supersededValidUntil": "2026-07"}, DreamDecision
    )
    assert decision.superseded_valid_until is not None
    assert decision.superseded_valid_until.year == 2026


async def test_validation_tolerates_unparseable_dates():
    """An absent validity bound is harmless; a hallucinated one distorts
    temporal queries."""
    decision = validate_response(
        {"action": "SUPERSEDE", "supersededValidUntil": "last month"}, DreamDecision
    )
    assert decision.superseded_valid_until is None


async def test_schema_hint_is_compact_and_complete():
    hint = schema_hint(ExtractionResult)
    assert "hasMemory" in hint
    assert "memories" in hint
    # Prose is stripped: the prompt already explains the task.
    assert "description" not in hint


# --------------------------------------------------------------------------- #
# The repair round-trip
# --------------------------------------------------------------------------- #


class _StubProvider:
    """Exercises the shared `generate_json` with `_call` stubbed.

    Targets `OpenAIStyleLLMProvider` — the base every OpenAI-wire-compatible
    provider inherits — rather than one concrete subclass, because the retry and
    repair logic under test is the shared code, not Azure's client construction.
    """

    @staticmethod
    def build(responses: list[str]):
        from app.core.config import LLMSettings
        from app.providers.llm._openai_style import OpenAIStyleLLMProvider

        provider = OpenAIStyleLLMProvider.__new__(OpenAIStyleLLMProvider)
        provider._settings = LLMSettings()
        provider._model = "test-model"
        provider._label = "test"
        provider._supports_json_mode = True
        provider._client = None

        calls: list[list[dict]] = []

        async def _call(messages, temperature, max_tokens):
            calls.append(messages)
            return responses.pop(0), {"total_tokens": 10}

        provider._call = _call
        return provider, calls


async def test_valid_first_response_needs_no_repair():
    provider, calls = _StubProvider.build(['{"action": "ADD", "reason": "new"}'])

    result = await provider.generate_json(
        system="s", user="u", response_model=DreamDecision
    )

    assert result.data.action.value == "ADD"
    assert result.repaired is False
    assert len(calls) == 1


async def test_invalid_first_response_is_repaired():
    provider, calls = _StubProvider.build(
        [
            '{"action": "NOT_AN_ACTION"}',        # fails validation
            '{"action": "ADD", "reason": "ok"}',  # repaired
        ]
    )

    result = await provider.generate_json(
        system="s", user="u", response_model=DreamDecision
    )

    assert result.data.action.value == "ADD"
    assert result.repaired is True
    assert len(calls) == 2

    # The repair turn feeds back the failed output and the validation error.
    repair_messages = calls[1]
    assert repair_messages[-2]["role"] == "assistant"
    assert repair_messages[-2]["content"] == '{"action": "NOT_AN_ACTION"}'
    assert "rejected by schema validation" in repair_messages[-1]["content"]

    # Token usage is summed across both turns, not overwritten by the second.
    assert result.usage["total_tokens"] == 20


async def test_two_bad_responses_raise_rather_than_returning_garbage():
    provider, _ = _StubProvider.build(
        ['{"action": "NOPE"}', '{"action": "STILL_NOPE"}']
    )

    with pytest.raises(LLMResponseError):
        await provider.generate_json(system="s", user="u", response_model=DreamDecision)


async def test_unparseable_first_response_is_also_repaired():
    provider, calls = _StubProvider.build(
        ["I'm sorry, I can't do that.", '{"action": "NOOP", "reason": "fine"}']
    )

    result = await provider.generate_json(
        system="s", user="u", response_model=DreamDecision
    )

    assert result.data.action.value == "NOOP"
    assert result.repaired is True
    assert len(calls) == 2


async def test_system_message_carries_the_schema_contract():
    provider, calls = _StubProvider.build(['{"action": "ADD"}'])

    await provider.generate_json(
        system="You decide things.", user="{}", response_model=DreamDecision
    )

    system_message = calls[0][0]["content"]
    assert "You decide things." in system_message
    assert "single JSON object" in system_message
    assert "targetMemoryIds" in system_message
