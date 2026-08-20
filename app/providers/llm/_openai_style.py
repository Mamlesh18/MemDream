"""Shared implementation for every provider that speaks the OpenAI wire format.

Azure OpenAI, OpenAI, Groq, Ollama, Together, Fireworks and most hosted
inference services expose the same `chat/completions` shape. Only two things
differ between them: how the client is constructed, and what the model is called.

Everything else — retry policy, JSON-mode negotiation, token accounting, the
schema-repair round-trip — is identical, so it lives here once. Two copies of
200 lines of retry logic is how the copies quietly drift apart, and the one that
gets fixed is never the one in production.

Subclasses supply a configured `AsyncOpenAI`-compatible client and a label.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, TypeVar

from pydantic import BaseModel

from app.core.config import LLMSettings
from app.core.exceptions import LLMResponseError, ProviderError
from app.core.logging import get_logger
from app.core.metrics import (
    LLM_CALLS,
    LLM_COMPLETION_TOKENS,
    LLM_ERRORS,
    LLM_LATENCY,
    LLM_PROMPT_TOKENS,
    LLM_TOKENS,
    current_operation,
    metrics,
)
from app.providers.llm.base import (
    LLMProvider,
    LLMResult,
    extract_json_object,
    repair_instruction,
    schema_hint,
    validate_response,
)

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

#: Transient failures worth retrying: rate limits, timeouts, 5xx.
_RETRYABLE_MARKERS = (
    "rate limit",
    "429",
    "timeout",
    "timed out",
    "connection",
    "temporarily unavailable",
    "overloaded",
    "500",
    "502",
    "503",
    "504",
)


def is_retryable(error: Exception) -> bool:
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    message = str(error).lower()
    return any(marker in message for marker in _RETRYABLE_MARKERS)


class OpenAIStyleLLMProvider(LLMProvider):
    """Chat completions with validated JSON output.

    JSON-object response format is requested when the endpoint supports it, and
    the requirement is also stated in the prompt so that endpoints without it
    still produce parseable output. Every response is validated against the
    caller's Pydantic model, with one repair round-trip before giving up.
    """

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        label: str,
        settings: LLMSettings | None = None,
        supports_json_mode: bool = True,
    ) -> None:
        self._client = client
        self._model = model
        #: Used in metric labels and error messages, e.g. "groq", "azure_openai".
        self._label = label
        self._settings = settings or LLMSettings()
        # Negotiated at runtime: an endpoint that rejects `response_format` has
        # it disabled permanently rather than failing every subsequent call.
        self._supports_json_mode = supports_json_mode

    @property
    def model_name(self) -> str:
        return f"{self._label}:{self._model}"

    # --- transport ------------------------------------------------------------ #

    async def _call(
        self, messages: list[dict[str, str]], temperature: float, max_tokens: int
    ) -> tuple[str, dict[str, int]]:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self._supports_json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(self._settings.max_retries):
            started = time.perf_counter()
            try:
                response = await self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - normalised into ProviderError below
                last_error = exc
                message = str(exc).lower()
                if self._supports_json_mode and "response_format" in message:
                    # This endpoint predates JSON mode, or does not implement it.
                    # Fall back to prompt-only JSON discipline for the rest of
                    # this process rather than failing every call.
                    logger.warning(
                        "llm_json_mode_unsupported",
                        provider=self._label,
                        model=self._model,
                        detail=str(exc)[:200],
                    )
                    self._supports_json_mode = False
                    kwargs.pop("response_format", None)
                    continue
                if not is_retryable(exc) or attempt == self._settings.max_retries - 1:
                    break
                backoff = min(2.0**attempt, 8.0)
                logger.warning(
                    "llm_retry",
                    provider=self._label,
                    attempt=attempt + 1,
                    backoff_seconds=backoff,
                    error=str(exc)[:200],
                )
                await asyncio.sleep(backoff)
                continue

            latency_ms = (time.perf_counter() - started) * 1000.0
            labels = {"provider": self._label, "operation": current_operation()}
            metrics.observe(LLM_LATENCY, latency_ms, labels)
            metrics.increment(LLM_CALLS, labels=labels)

            usage: dict[str, int] = {}
            raw_usage = getattr(response, "usage", None)
            if raw_usage is not None:
                usage = {
                    "prompt_tokens": int(getattr(raw_usage, "prompt_tokens", 0) or 0),
                    "completion_tokens": int(getattr(raw_usage, "completion_tokens", 0) or 0),
                    "total_tokens": int(getattr(raw_usage, "total_tokens", 0) or 0),
                }
                # Split, because input and output are priced differently — an
                # aggregate cannot be turned back into a cost.
                metrics.increment(LLM_PROMPT_TOKENS, float(usage["prompt_tokens"]), labels)
                metrics.increment(
                    LLM_COMPLETION_TOKENS, float(usage["completion_tokens"]), labels
                )
                metrics.increment(LLM_TOKENS, float(usage["total_tokens"]), labels)

            if not response.choices:
                raise LLMResponseError(f"{self._label} returned no choices")

            choice = response.choices[0]
            if getattr(choice, "finish_reason", None) == "length":
                # Truncated JSON never parses; say why rather than surfacing a
                # confusing "unterminated object" further down.
                raise LLMResponseError(
                    "Model output was truncated by max_tokens. "
                    "Raise DREAM_LLM__MAX_OUTPUT_TOKENS.",
                    details={"max_tokens": max_tokens, "provider": self._label},
                )
            return (choice.message.content or ""), usage

        metrics.increment(LLM_ERRORS, labels={"provider": self._label})
        raise ProviderError(
            f"{self._label} chat request failed: {last_error}",
            details={"model": self._model},
        ) from last_error

    # --- API -------------------------------------------------------------------- #

    async def generate_json(
        self,
        *,
        system: str,
        user: str,
        response_model: type[T],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult[T]:
        temperature = self._settings.temperature if temperature is None else temperature
        max_tokens = max_tokens or self._settings.max_output_tokens

        system_message = (
            f"{system}\n\n"
            "## Output contract\n"
            "Reply with a single JSON object and nothing else — no prose, no code fences.\n"
            f"It must validate against this JSON Schema:\n{schema_hint(response_model)}"
        )
        messages = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user},
        ]

        started = time.perf_counter()
        content, usage = await self._call(messages, temperature, max_tokens)

        try:
            data = validate_response(extract_json_object(content), response_model)
            return LLMResult(
                data=data,
                model=self.model_name,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                usage=usage,
                raw=content,
            )
        except LLMResponseError as exc:
            # Bound to a local because Python unbinds an `except ... as` name
            # when the block exits — referring to it below would raise NameError
            # on exactly the path that is already going wrong.
            failure = str(exc)
            logger.warning(
                "llm_response_repair",
                provider=self._label,
                model=self.model_name,
                schema=response_model.__name__,
                error=failure[:300],
            )

        # One repair attempt, with the validation error fed back in.
        messages.append({"role": "assistant", "content": content})
        messages.append(
            {"role": "user", "content": repair_instruction(response_model, failure)}
        )
        repaired_content, repair_usage = await self._call(messages, 0.0, max_tokens)

        for key, value in repair_usage.items():
            usage[key] = usage.get(key, 0) + value

        data = validate_response(extract_json_object(repaired_content), response_model)
        return LLMResult(
            data=data,
            model=self.model_name,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            usage=usage,
            raw=repaired_content,
            repaired=True,
        )

    async def close(self) -> None:
        await self._client.close()
