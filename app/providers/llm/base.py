"""The LLM provider contract.

There is exactly one method, and it returns a validated Pydantic model. The
Dream engine never sees free-form text: if the model cannot produce something
that satisfies the schema, that is a provider error, not a parsing problem for
the caller to solve.

Why JSON-object mode rather than strict schema mode: strict structured outputs
require every field to be required and `additionalProperties: false`, which
fights against models with sensible defaults, and support varies by deployment
and API version. Prompt-embedded schema + validation + a single repair round-trip
works on every Azure deployment and fails loudly when it does not.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.core.exceptions import LLMResponseError

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass(slots=True)
class LLMResult(Generic[T]):
    """A validated response plus everything the audit log wants to know."""

    data: T
    model: str
    latency_ms: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)
    raw: str = ""
    #: True when the first response failed validation and a repair round-trip
    #: was needed. Worth alerting on if it becomes common.
    repaired: bool = False


class LLMProvider(ABC):
    """Structured-output language model."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Identifier recorded on every audit row."""

    @abstractmethod
    async def generate_json(
        self,
        *,
        system: str,
        user: str,
        response_model: type[T],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult[T]:
        """Return an instance of ``response_model``.

        Implementations must raise `LLMResponseError` rather than returning a
        partially-valid object: the engine's safety argument rests on every
        decision it executes having been schema-checked.
        """

    async def close(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True


# --------------------------------------------------------------------------- #
# Parsing helpers, shared by implementations
# --------------------------------------------------------------------------- #


def extract_json_object(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model response.

    Handles the three things models actually do: return clean JSON, wrap it in a
    ``json`` fence, or precede it with a sentence of explanation.
    """
    if not text or not text.strip():
        raise LLMResponseError("Model returned an empty response")

    candidate = text.strip()

    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Brace matching, string-aware so a `{` inside a value cannot end the scan.
    start = candidate.find("{")
    if start == -1:
        raise LLMResponseError(f"No JSON object in model response: {candidate[:200]!r}")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(candidate)):
        char = candidate[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                fragment = candidate[start : index + 1]
                try:
                    parsed = json.loads(fragment)
                except json.JSONDecodeError as exc:
                    raise LLMResponseError(
                        f"Malformed JSON in model response: {exc}"
                    ) from exc
                if not isinstance(parsed, dict):
                    raise LLMResponseError("Model returned JSON that is not an object")
                return parsed

    raise LLMResponseError(f"Unterminated JSON object in response: {candidate[:200]!r}")


def validate_response(payload: dict[str, Any], response_model: type[T]) -> T:
    try:
        return response_model.model_validate(payload)
    except PydanticValidationError as exc:
        raise LLMResponseError(
            f"Model response does not satisfy {response_model.__name__}: {exc}",
            details={"payload": payload},
        ) from exc


def schema_hint(response_model: type[BaseModel]) -> str:
    """A compact JSON Schema to paste into the system prompt.

    Titles and descriptions are stripped — the prompt already explains the task
    in prose, and repeating it inside the schema wastes tokens and invites the
    model to echo documentation back as field values.
    """
    schema = response_model.model_json_schema(by_alias=True)

    def prune(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                key: prune(value)
                for key, value in node.items()
                if key not in ("title", "description", "$defs", "examples")
            }
        if isinstance(node, list):
            return [prune(item) for item in node]
        return node

    definitions = schema.get("$defs", {})
    pruned = prune(schema)
    if definitions:
        pruned["definitions"] = {name: prune(body) for name, body in definitions.items()}
    return json.dumps(pruned, separators=(",", ":"))


def repair_instruction(response_model: type[BaseModel], error: str) -> str:
    """The follow-up message sent when the first response failed validation."""
    return (
        "Your previous response was rejected by schema validation.\n\n"
        f"Error:\n{error[:1200]}\n\n"
        f"Required JSON Schema:\n{schema_hint(response_model)}\n\n"
        "Reply with ONLY the corrected JSON object. No prose, no code fences."
    )
