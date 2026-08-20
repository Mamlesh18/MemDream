"""Google Gemini.

    pip install memdream[gemini]

Gemini exposes an OpenAI-compatible endpoint, and using it would have been the
cheap option — but it is a compatibility shim, and the native SDK gives two
things that matter here:

* **`response_mime_type="application/json"`**, which is a real constraint on
  decoding rather than an instruction the model may ignore. Since every Dream
  decision must satisfy a Pydantic schema, guaranteed-parseable output removes
  an entire class of repair round-trip.
* **Accurate token accounting** from `usage_metadata`, which the compatibility
  layer does not always populate — and token cost is reported per operation
  here, so silently-zero usage would be worse than none.

Gemini also has an embeddings API, so it can serve both roles. See
`embeddings/gemini.py`.
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

DEFAULT_MODEL = "gemini-2.0-flash"


class GeminiLLMProvider(LLMProvider):
    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        settings: LLMSettings | None = None,
    ) -> None:
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ProviderError(
                "google-genai is required. Install with: pip install memdream[gemini]"
            ) from exc

        if not api_key:
            raise ProviderError(
                "Gemini requires an API key. Set DREAM_GEMINI_API_KEY or export "
                "GOOGLE_API_KEY."
            )

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._settings = settings or LLMSettings()

    @property
    def model_name(self) -> str:
        return f"gemini:{self._model}"

    # --- transport ------------------------------------------------------------ #

    async def _call(self, system: str, user: str, temperature: float, max_tokens: int):
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            max_output_tokens=max_tokens,
            # A decoding constraint, not a request. The model cannot emit prose.
            response_mime_type="application/json",
        )

        last_error: Exception | None = None
        for attempt in range(self._settings.max_retries):
            started = time.perf_counter()
            try:
                response = await self._client.aio.models.generate_content(
                    model=self._model, contents=user, config=config
                )
            except Exception as exc:  # noqa: BLE001 - normalised below
                last_error = exc
                if not is_retryable(exc) or attempt == self._settings.max_retries - 1:
                    break
                backoff = min(2.0**attempt, 8.0)
                logger.warning(
                    "llm_retry", provider="gemini", attempt=attempt + 1,
                    backoff_seconds=backoff, error=str(exc)[:200],
                )
                await asyncio.sleep(backoff)
                continue

            labels = {"provider": "gemini", "operation": current_operation()}
            metrics.observe(LLM_LATENCY, (time.perf_counter() - started) * 1000.0, labels)
            metrics.increment(LLM_CALLS, labels=labels)

            usage: dict[str, int] = {}
            raw_usage = getattr(response, "usage_metadata", None)
            if raw_usage is not None:
                usage = {
                    "prompt_tokens": int(getattr(raw_usage, "prompt_token_count", 0) or 0),
                    "completion_tokens": int(
                        getattr(raw_usage, "candidates_token_count", 0) or 0
                    ),
                }
                usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
                metrics.increment(LLM_PROMPT_TOKENS, float(usage["prompt_tokens"]), labels)
                metrics.increment(
                    LLM_COMPLETION_TOKENS, float(usage["completion_tokens"]), labels
                )
                metrics.increment(LLM_TOKENS, float(usage["total_tokens"]), labels)

            text = getattr(response, "text", None)
            if not text:
                # Usually a safety block or an empty candidate list. Say which,
                # rather than surfacing "no JSON found" further down.
                feedback = getattr(response, "prompt_feedback", None)
                raise LLMResponseError(
                    f"Gemini returned no text (feedback: {feedback})",
                    details={"model": self._model},
                )
            return text, usage

        metrics.increment(LLM_ERRORS, labels={"provider": "gemini"})
        raise ProviderError(
            f"Gemini request failed: {last_error}", details={"model": self._model}
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
            "Reply with a single JSON object and nothing else.\n"
            f"It must validate against this JSON Schema:\n{schema_hint(response_model)}"
        )

        started = time.perf_counter()
        content, usage = await self._call(system_message, user, temperature, max_tokens)

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
                "llm_response_repair", provider="gemini",
                schema=response_model.__name__, error=failure[:300],
            )

        # One repair attempt, with the validation error fed back in.
        repaired, repair_usage = await self._call(
            system_message,
            f"{user}\n\n{repair_instruction(response_model, failure)}",
            0.0,
            max_tokens,
        )
        for key, value in repair_usage.items():
            usage[key] = usage.get(key, 0) + value

        data = validate_response(extract_json_object(repaired), response_model)
        return LLMResult(
            data=data,
            model=self.model_name,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            usage=usage,
            raw=repaired,
            repaired=True,
        )


def from_settings(settings: Settings) -> GeminiLLMProvider:
    return GeminiLLMProvider(
        api_key=settings.gemini_api_key,
        model=settings.gemini_chat_model or DEFAULT_MODEL,
        settings=settings.llm,
    )
