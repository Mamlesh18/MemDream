"""Anthropic Claude.

    pip install memdream[anthropic]

Claude has no JSON mode, so structured output is obtained differently: the
assistant turn is **prefilled with an opening brace**. The model can only
continue from there, which makes prose impossible rather than merely discouraged
— the same guarantee JSON mode gives elsewhere, achieved through the API's own
mechanics.

The brace is prepended back onto the response before parsing, since the model
returns only its continuation.

Note that Anthropic offers no embeddings API, so this provider serves chat only.
Pair it with OpenAI, Azure, Gemini or Ollama for embeddings — the two are
configured independently for exactly this reason.
"""

from __future__ import annotations

import asyncio
import time
from typing import TypeVar

from pydantic import BaseModel

from app.core.config import LLMSettings, Settings
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
from app.providers.llm._openai_style import is_retryable
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

DEFAULT_MODEL = "claude-sonnet-4-5"

#: Prefilling the assistant turn with this leaves the model no way to begin with
#: prose. It is Anthropic's documented technique for structured output.
_JSON_PREFILL = "{"


class AnthropicLLMProvider(LLMProvider):
    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        settings: LLMSettings | None = None,
    ) -> None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ProviderError(
                "anthropic is required. Install with: pip install memdream[anthropic]"
            ) from exc

        if not api_key:
            raise ProviderError(
                "Anthropic requires an API key. Set DREAM_ANTHROPIC_API_KEY or "
                "export ANTHROPIC_API_KEY."
            )

        self._settings = settings or LLMSettings()
        self._client = AsyncAnthropic(
            api_key=api_key,
            timeout=self._settings.timeout_seconds,
            # Retries are handled here so they can be counted and logged.
            max_retries=0,
        )
        self._model = model

    @property
    def model_name(self) -> str:
        return f"anthropic:{self._model}"

    # --- transport ------------------------------------------------------------ #

    async def _call(
        self, system: str, messages: list[dict], temperature: float, max_tokens: int
    ) -> tuple[str, dict[str, int]]:
        last_error: Exception | None = None
        for attempt in range(self._settings.max_retries):
            started = time.perf_counter()
            try:
                response = await self._client.messages.create(
                    model=self._model,
                    system=system,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception as exc:  # noqa: BLE001 - normalised below
                last_error = exc
                if not is_retryable(exc) or attempt == self._settings.max_retries - 1:
                    break
                backoff = min(2.0**attempt, 8.0)
                logger.warning(
                    "llm_retry", provider="anthropic", attempt=attempt + 1,
                    backoff_seconds=backoff, error=str(exc)[:200],
                )
                await asyncio.sleep(backoff)
                continue

            labels = {"provider": "anthropic", "operation": current_operation()}
            metrics.observe(LLM_LATENCY, (time.perf_counter() - started) * 1000.0, labels)
            metrics.increment(LLM_CALLS, labels=labels)

            usage: dict[str, int] = {}
            raw_usage = getattr(response, "usage", None)
            if raw_usage is not None:
                usage = {
                    "prompt_tokens": int(getattr(raw_usage, "input_tokens", 0) or 0),
                    "completion_tokens": int(getattr(raw_usage, "output_tokens", 0) or 0),
                }
                usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
                metrics.increment(LLM_PROMPT_TOKENS, float(usage["prompt_tokens"]), labels)
                metrics.increment(
                    LLM_COMPLETION_TOKENS, float(usage["completion_tokens"]), labels
                )
                metrics.increment(LLM_TOKENS, float(usage["total_tokens"]), labels)

            if getattr(response, "stop_reason", None) == "max_tokens":
                # Truncated JSON never parses; name the cause rather than letting
                # it surface as "unterminated object".
                raise LLMResponseError(
                    "Model output was truncated by max_tokens. "
                    "Raise DREAM_LLM__MAX_OUTPUT_TOKENS.",
                    details={"max_tokens": max_tokens, "provider": "anthropic"},
                )

            text = "".join(
                block.text for block in response.content if getattr(block, "type", "") == "text"
            )
            if not text:
                raise LLMResponseError("Anthropic returned no text content")
            return text, usage

        metrics.increment(LLM_ERRORS, labels={"provider": "anthropic"})
        raise ProviderError(
            f"Anthropic request failed: {last_error}", details={"model": self._model}
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
            {"role": "user", "content": user},
            # Prefill: the model continues from the brace, so it cannot open
            # with an explanation.
            {"role": "assistant", "content": _JSON_PREFILL},
        ]

        started = time.perf_counter()
        continuation, usage = await self._call(
            system_message, messages, temperature, max_tokens
        )
        content = _JSON_PREFILL + continuation

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
            failure = str(exc)
            logger.warning(
                "llm_response_repair", provider="anthropic",
                schema=response_model.__name__, error=failure[:300],
            )

        repair_messages = [
            {"role": "user", "content": user},
            {"role": "assistant", "content": content},
            {"role": "user", "content": repair_instruction(response_model, failure)},
            {"role": "assistant", "content": _JSON_PREFILL},
        ]
        repaired, repair_usage = await self._call(
            system_message, repair_messages, 0.0, max_tokens
        )
        repaired_content = _JSON_PREFILL + repaired

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


def from_settings(settings: Settings) -> AnthropicLLMProvider:
    return AnthropicLLMProvider(
        api_key=settings.anthropic_api_key,
        model=settings.anthropic_model or DEFAULT_MODEL,
        settings=settings.llm,
    )
