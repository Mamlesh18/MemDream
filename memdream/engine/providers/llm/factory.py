from __future__ import annotations

from memdream.engine.core.config import Settings, get_settings
from memdream.engine.core.exceptions import ConfigurationError
from memdream.engine.providers.llm.base import LLMProvider


def build_llm_provider(settings: Settings | None = None) -> LLMProvider:
    """Construct the LLM provider named by ``DREAM_LLM_PROVIDER``."""
    settings = settings or get_settings()
    name = settings.llm_provider

    if name == "azure_openai":
        from memdream.engine.providers.llm.azure_openai import from_settings

        return from_settings(settings)

    if name in ("openai", "ollama", "together", "fireworks", "openai_compatible"):
        from memdream.engine.providers.llm.openai_compatible import from_settings

        return from_settings(settings, name)

    # Gemini and Anthropic each need their own SDK: neither speaks the OpenAI
    # wire format well enough for structured output to be guaranteed.
    if name == "gemini":
        from memdream.engine.providers.llm.gemini import from_settings

        return from_settings(settings)

    if name == "anthropic":
        from memdream.engine.providers.llm.anthropic import from_settings

        return from_settings(settings)

    if name == "fake":
        from memdream.engine.providers.llm.fake import FakeLLMProvider

        return FakeLLMProvider()

    raise ConfigurationError(f"Unknown LLM provider: {name!r}")
