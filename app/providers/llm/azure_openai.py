"""Azure OpenAI chat completions with validated JSON output."""

from __future__ import annotations

import asyncio
import time
from typing import Any, TypeVar

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


def _is_retryable(error: Exception) -> bool:
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    message = str(error).lower()
    return any(marker in message for marker in _RETRYABLE_MARKERS)


class AzureOpenAILLMProvider(LLMProvider):
    """Chat completions against an Azure OpenAI deployment.

    JSON-object response format is requested when the deployment supports it,
    and the requirement is also stated in the prompt so that older deployments
    still produce parseable output. Every response is validated against the
    caller's Pydantic model, with one repair round-trip before giving up.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        api_version: str,
        deployment: str,
        settings: LLMSettings | None = None,
    ) -> None:
        try:
            from openai import AsyncAzureOpenAI
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ProviderError(
                "The 'openai' package is required for the Azure OpenAI provider"
            ) from exc

        if not endpoint or not api_key:
            raise ProviderError(
                "AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY must both be set"
            )

        self._settings = settings or LLMSettings()
        self._deployment = deployment
        self._supports_json_mode = True
        self._client = AsyncAzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
            timeout=self._settings.timeout_seconds,
            max_retries=0,
        )

    @property
    def model_name(self) -> str:
        return f"azure-openai:{self._deployment}"

    # --- transport ------------------------------------------------------------ #

    async def _call(
        self, messages: list[dict[str, str]], temperature: float, max_tokens: int
    ) -> tuple[str, dict[str, int]]:
        kwargs: dict[str, Any] = {
            "model": self._deployment,
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
            except Exception as exc:  # noqa: BLE001 - normalised below
                last_error = exc
                message = str(exc).lower()
                if self._supports_json_mode and "response_format" in message:
                    # Older deployment: fall back to prompt-only JSON discipline.
                    logger.warning(
                        "llm_json_mode_unsupported",
                        deployment=self._deployment,
                        detail=str(exc)[:200],
                    )
                    self._supports_json_mode = False
                    kwargs.pop("response_format", None)
                    continue
                if not _is_retryable(exc) or attempt == self._settings.max_retries - 1:
                    break
                backoff = min(2.0**attempt, 8.0)
                logger.warning(
                    "llm_retry",
                    attempt=attempt + 1,
                    backoff_seconds=backoff,
                    error=str(exc)[:200],
                )
                await asyncio.sleep(backoff)
                continue

            latency_ms = (time.perf_counter() - started) * 1000.0
            labels = {"provider": "azure_openai", "operation": current_operation()}
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
                raise LLMResponseError("Azure OpenAI returned no choices")

            choice = response.choices[0]
            if getattr(choice, "finish_reason", None) == "length":
                # Truncated JSON never parses; say why rather than surfacing a
                # confusing "unterminated object" further down.
                raise LLMResponseError(
                    "Model output was truncated by max_tokens. "
                    "Raise DREAM_LLM__MAX_OUTPUT_TOKENS.",
                    details={"max_tokens": max_tokens},
                )
            return (choice.message.content or ""), usage

        metrics.increment(LLM_ERRORS, labels={"provider": "azure_openai"})
        raise ProviderError(
            f"Azure OpenAI chat request failed: {last_error}",
            details={"deployment": self._deployment},
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
            # Bound to a local here because Python unbinds an `except ... as`
            # name when the block exits — referring to it below would raise
            # NameError on exactly the path that is already going wrong.
            failure = str(exc)
            logger.warning(
                "llm_response_repair",
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


def from_settings(settings: Settings) -> AzureOpenAILLMProvider:
    settings.require_azure_openai()
    return AzureOpenAILLMProvider(
        endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
        deployment=settings.azure_openai_chat_deployment,
        settings=settings.llm,
    )
