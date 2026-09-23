"""One-line provider selection for the SDK.

Choosing models through settings means knowing which of six fields a provider
reads: OpenAI takes `openai_chat_model`, Azure takes a *deployment* name, Gemini
and Anthropic take their own. That is fine in a `.env` file written once, and
tedious in code.

So the SDK accepts a shorthand instead:

    MemDream(
        llm="openai:gpt-4o-mini",
        embeddings="openai:text-embedding-3-small",
        vector="faiss:./data/faiss",
        database="mongodb://localhost:27017/memdream",
        prompts="./prompts",
    )

Each expands to the settings fields that provider actually reads. Anything not
expressible here — timeouts, retry counts, tuning thresholds — is still a plain
settings keyword, and the two mix freely.

**Credentials are deliberately not part of this.** `llm="openai:sk-...:gpt-4o"`
would put a key in source code, so keys stay in the environment
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AZURE_OPENAI_API_KEY`, …).
"""

from __future__ import annotations

from typing import Any

from app.core.exceptions import ConfigurationError

#: Spellings people reach for, mapped to the provider MemDream knows.
ALIASES: dict[str, str] = {
    "azure": "azure_openai",
    "azureopenai": "azure_openai",
    "claude": "anthropic",
    "google": "gemini",
    "googleai": "gemini",
    "vllm": "openai_compatible",
    "litellm": "openai_compatible",
    "compatible": "openai_compatible",
    "custom": "openai_compatible",
}

#: Which field holds the chat model, per provider. Everything that speaks the
#: OpenAI wire format shares one set of fields.
LLM_MODEL_FIELD: dict[str, str] = {
    "openai": "openai_chat_model",
    "groq": "openai_chat_model",
    "ollama": "openai_chat_model",
    "together": "openai_chat_model",
    "fireworks": "openai_chat_model",
    "openai_compatible": "openai_chat_model",
    "azure_openai": "azure_openai_chat_deployment",
    "gemini": "gemini_chat_model",
    "anthropic": "anthropic_model",
    "fake": "",
}

EMBEDDING_MODEL_FIELD: dict[str, str] = {
    "openai": "openai_embedding_model",
    "ollama": "openai_embedding_model",
    "openai_compatible": "openai_embedding_model",
    "azure_openai": "azure_openai_embedding_deployment",
    "gemini": "gemini_embedding_model",
    "fake": "",
}

EMBEDDING_DIMENSION_FIELD: dict[str, str] = {
    "openai": "openai_embedding_dimensions",
    "ollama": "openai_embedding_dimensions",
    "openai_compatible": "openai_embedding_dimensions",
    "azure_openai": "azure_openai_embedding_dimensions",
    "gemini": "gemini_embedding_dimensions",
}

#: Where a vector store's one positional argument goes.
VECTOR_TARGET_FIELD: dict[str, str] = {
    "faiss": "faiss_path",
    "qdrant": "qdrant_url",
    "pgvector": "postgres_dsn",
    "chroma": "chroma_path",
    "memory": "",
    "pinecone": "",
    "milvus": "",
    "azure_search": "",
}

#: The keywords this module understands.
SHORTHAND_KEYS: tuple[str, ...] = ("llm", "embeddings", "vector", "database", "prompts")


def _split(value: str, kind: str) -> tuple[str, str]:
    provider, _, rest = value.partition(":")
    provider = provider.strip().lower().replace("-", "_")
    provider = ALIASES.get(provider.replace("_", ""), ALIASES.get(provider, provider))
    if not provider:
        raise ConfigurationError(f"{kind} shorthand is empty. Expected 'provider:model'.")
    return provider, rest.strip()


def _known(provider: str, table: dict[str, str], kind: str) -> None:
    if provider not in table:
        raise ConfigurationError(
            f"Unknown {kind} provider {provider!r}. "
            f"Choose one of: {', '.join(sorted(table))}",
            details={"provider": provider},
        )


def expand(overrides: dict[str, Any]) -> dict[str, Any]:
    """Turn shorthand keywords into settings fields.

    Keywords this module does not own are passed through untouched, so
    `MemDream(llm="openai:gpt-4o-mini", log_level="DEBUG")` works.
    """
    resolved = {key: value for key, value in overrides.items() if key not in SHORTHAND_KEYS}

    llm = overrides.get("llm")
    if llm:
        provider, model = _split(str(llm), "llm")
        _known(provider, LLM_MODEL_FIELD, "LLM")
        resolved["llm_provider"] = provider
        field = LLM_MODEL_FIELD[provider]
        if model and field:
            resolved[field] = model

    embeddings = overrides.get("embeddings")
    if embeddings:
        provider, rest = _split(str(embeddings), "embeddings")
        _known(provider, EMBEDDING_MODEL_FIELD, "embedding")
        resolved["embedding_provider"] = provider
        model, _, dimensions = rest.partition(":")
        field = EMBEDDING_MODEL_FIELD[provider]
        if model and field:
            resolved[field] = model
        if dimensions:
            if not dimensions.isdigit():
                raise ConfigurationError(
                    f"Embedding width must be a number, got {dimensions!r}. "
                    "Expected 'provider:model' or 'provider:model:dimensions'."
                )
            resolved[EMBEDDING_DIMENSION_FIELD[provider]] = int(dimensions)

    vector = overrides.get("vector")
    if vector:
        provider, target = _split(str(vector), "vector")
        _known(provider, VECTOR_TARGET_FIELD, "vector store")
        resolved["vector_provider"] = provider
        field = VECTOR_TARGET_FIELD[provider]
        if target and field:
            # Chroma is a library or a server depending on what you point it at.
            if provider == "chroma" and target.startswith(("http://", "https://")):
                resolved["chroma_host"] = target
            else:
                resolved[field] = target

    database = overrides.get("database")
    # A connected database object is a different feature (`database=` on the
    # SDK); only a connection string is shorthand.
    if isinstance(database, str) and database:
        resolved.update(_database(database))

    prompts = overrides.get("prompts")
    if isinstance(prompts, str) and prompts:
        resolved["prompts_dir"] = prompts
    elif isinstance(prompts, dict) and prompts:
        resolved["prompts"] = prompts

    return resolved


def _database(dsn: str) -> dict[str, Any]:
    if dsn.startswith(("mongodb://", "mongodb+srv://")):
        fields: dict[str, Any] = {"database_provider": "mongodb", "mongodb_uri": dsn}
        # mongodb://host:27017/memdream — the path names the database, which is
        # how everyone writes a Mongo URI, even though the driver ignores it.
        tail = dsn.split("://", 1)[1]
        if "/" in tail:
            name = tail.split("/", 1)[1].split("?")[0].strip()
            if name:
                fields["mongodb_database"] = name
        return fields
    if dsn.startswith(("postgresql://", "postgres://")):
        return {"database_provider": "postgres", "postgres_dsn": dsn}
    raise ConfigurationError(
        f"Unrecognised database connection string {dsn.split('://')[0]!r}. "
        "Expected one starting mongodb://, mongodb+srv://, or postgresql://.",
    )
