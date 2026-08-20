"""OpenAI, Groq, Ollama, Together, Fireworks — and anything else OpenAI-shaped.

These are not five integrations. They are one integration with five base URLs.
Every service here implements the same `chat/completions` contract, so the only
thing that varies is where to send the request, which model to name, and where
the API key lives.

That is what the preset table below is: a lookup so `DREAM_LLM_PROVIDER=groq`
works without the operator having to know Groq's URL. Point `base_url` at
anything else OpenAI-shaped and it works too, named or not.

Ollama matters disproportionately here: it means MemDream runs fully local, with
no API key and no per-token cost.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.core.exceptions import ProviderError
from app.core.logging import get_logger
from app.providers.llm._openai_style import OpenAIStyleLLMProvider

logger = get_logger(__name__)


@dataclass(frozen=True)
class Preset:
    """Defaults for one named OpenAI-compatible service."""

    base_url: str
    chat_model: str
    #: Conventional environment variable, so an operator who has already exported
    #: OPENAI_API_KEY or GROQ_API_KEY does not have to re-declare it.
    api_key_env: str | None
    #: Locally hosted runtimes accept any placeholder key.
    requires_key: bool = True
    #: Not every endpoint implements `response_format: json_object`. Where it is
    #: known to be absent, skip the negotiation round-trip.
    supports_json_mode: bool = True


# Model names change often. These are starting points, not guarantees — set
# DREAM_OPENAI_CHAT_MODEL to pin whatever your account actually has.
PRESETS: dict[str, Preset] = {
    "openai": Preset(
        base_url="https://api.openai.com/v1",
        chat_model="gpt-4o-mini",
        api_key_env="OPENAI_API_KEY",
    ),
    "groq": Preset(
        base_url="https://api.groq.com/openai/v1",
        chat_model="llama-3.3-70b-versatile",
        api_key_env="GROQ_API_KEY",
    ),
    "ollama": Preset(
        base_url="http://localhost:11434/v1",
        chat_model="llama3.2",
        api_key_env=None,
        requires_key=False,
    ),
    "together": Preset(
        base_url="https://api.together.xyz/v1",
        chat_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        api_key_env="TOGETHER_API_KEY",
    ),
    "fireworks": Preset(
        base_url="https://api.fireworks.ai/inference/v1",
        chat_model="accounts/fireworks/models/llama-v3p3-70b-instruct",
        api_key_env="FIREWORKS_API_KEY",
    ),
    # Escape hatch: bring your own endpoint. Everything must be configured.
    "openai_compatible": Preset(
        base_url="", chat_model="", api_key_env=None, requires_key=False
    ),
}


class OpenAICompatibleLLMProvider(OpenAIStyleLLMProvider):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        label: str = "openai",
        settings: Settings | None = None,
        supports_json_mode: bool = True,
    ) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ProviderError("The 'openai' package is required") from exc

        llm_settings = (settings or Settings()).llm
        client = AsyncOpenAI(
            base_url=base_url or None,
            # Local runtimes ignore the key but the SDK requires a non-empty one.
            api_key=api_key or "not-needed",
            timeout=llm_settings.timeout_seconds,
            # Retries are handled upstream so they can be counted and logged.
            max_retries=0,
        )
        super().__init__(
            client=client,
            model=model,
            label=label,
            settings=llm_settings,
            supports_json_mode=supports_json_mode,
        )


def resolve(provider: str, settings: Settings) -> tuple[Preset, str, str, str]:
    """Merge a preset with explicit configuration. Explicit always wins.

    Returns ``(preset, base_url, api_key, model)``.
    """
    preset = PRESETS.get(provider)
    if preset is None:
        raise ProviderError(f"Unknown OpenAI-compatible provider: {provider!r}")

    base_url = settings.openai_base_url or preset.base_url
    model = settings.openai_chat_model or preset.chat_model
    api_key = settings.openai_api_key

    if not api_key and preset.api_key_env:
        import os

        api_key = os.environ.get(preset.api_key_env, "")

    if not base_url:
        raise ProviderError(
            f"{provider} needs a base URL. Set DREAM_OPENAI_BASE_URL."
        )
    if not model:
        raise ProviderError(
            f"{provider} needs a model name. Set DREAM_OPENAI_CHAT_MODEL."
        )
    if preset.requires_key and not api_key:
        env_hint = f" or export {preset.api_key_env}" if preset.api_key_env else ""
        raise ProviderError(
            f"{provider} requires an API key. Set DREAM_OPENAI_API_KEY{env_hint}, "
            "or switch DREAM_LLM_PROVIDER to 'ollama' to run locally with no key."
        )

    return preset, base_url, api_key, model


def from_settings(settings: Settings, provider: str) -> OpenAICompatibleLLMProvider:
    preset, base_url, api_key, model = resolve(provider, settings)
    logger.info("llm_provider_configured", provider=provider, model=model, base_url=base_url)
    return OpenAICompatibleLLMProvider(
        base_url=base_url,
        api_key=api_key,
        model=model,
        label=provider,
        settings=settings,
        supports_json_mode=preset.supports_json_mode,
    )
