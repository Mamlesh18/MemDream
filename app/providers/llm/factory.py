from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError
from app.providers.llm.base import LLMProvider


def build_llm_provider(settings: Settings | None = None) -> LLMProvider:
    """Construct the LLM provider named by ``DREAM_LLM_PROVIDER``."""
    settings = settings or get_settings()
    name = settings.llm_provider

    if name == "azure_openai":
        from app.providers.llm.azure_openai import from_settings

        return from_settings(settings)

    if name in ("openai", "groq", "ollama", "together", "fireworks", "openai_compatible"):
        from app.providers.llm.openai_compatible import from_settings

        return from_settings(settings, name)

    if name == "fake":
        from app.providers.llm.fake import FakeLLMProvider

        return FakeLLMProvider()

    raise ConfigurationError(f"Unknown LLM provider: {name!r}")
