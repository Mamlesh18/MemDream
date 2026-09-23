"""Prompts are configuration, and providers have a shorthand.

MemDream ships no prompts: the engine knows *how* to extract, decide and
synthesise, but what to say to the model depends on the product, so it is
supplied per deployment. Two things follow, and both are tested here.

**A deployment without prompts must not start.** Not start-and-fail-later: the
failure has to happen while the process is starting, naming every missing prompt
and how to supply it. A memory layer that boots and then NOOPs every message
looks like a tuning problem for a long time.

**Choosing models must not require knowing which of six fields a provider
reads.** `llm="openai:gpt-4o-mini"` expands to the right ones, and a provider
name nobody has is an error rather than a silent fallback to the default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.exceptions import ConfigurationError
from app.core.prompts import PROMPT_NAMES, PromptLibrary, required_prompts
from app.core.shorthand import expand

TEMPLATES = Path(__file__).resolve().parents[1] / "prompts"


def _settings(**overrides) -> Settings:
    """Settings built in isolation from the machine running the suite.

    `_env_file=None` matters here more than anywhere else: these tests assert on
    what is *absent*, and a developer .env carrying DREAM_PROMPTS_DIR or
    DREAM_DOMAIN would make "no prompts configured" quietly untestable — the
    suite would pass on their machine and fail in CI, or worse, the reverse.
    """
    overrides.setdefault("prompts_dir", "")
    overrides.setdefault("prompts", {})
    overrides.setdefault("domain", "")
    return Settings(env="test", identity_salt="t", _env_file=None, **overrides)


def _configured(**overrides) -> Settings:
    return _settings(prompts_dir=str(TEMPLATES), **overrides)


# --------------------------------------------------------------------------- #
# Nothing is built in
# --------------------------------------------------------------------------- #


def test_no_prompts_ship_with_the_package():
    """The point of the whole design: there is no default text to inherit."""
    with pytest.raises(ConfigurationError) as exc:
        PromptLibrary.from_settings(_settings())

    message = exc.value.message
    assert "ships no prompts" in message
    for name in ("extraction", "decision"):
        assert name in message


def test_the_error_says_every_way_to_fix_it():
    """This message is the first thing a new user sees, so it has to be the
    documentation for the thing it is complaining about."""
    with pytest.raises(ConfigurationError) as exc:
        PromptLibrary.from_settings(_settings())

    message = exc.value.message
    assert "python -m memdream prompts" in message, "the one-command fix"
    assert "DREAM_PROMPTS_DIR" in message, "the environment variable"
    assert "MemDream(prompts=" in message, "the SDK argument"
    assert "docs/prompts.md" in message, "where the contract is written down"


def test_every_missing_prompt_is_named_at_once(container):
    """Reporting them one per restart would mean five restarts."""
    with pytest.raises(ConfigurationError) as exc:
        PromptLibrary.from_settings(
            _settings(dream={"decision_mode": "staged", "compaction": {"enabled": True}})
        )

    assert set(exc.value.details["missing"]) == {
        "extraction", "supersede", "merge", "synthesis", "compaction",
    }


async def test_a_container_refuses_to_start_without_prompts(settings, database):
    """Startup, not the first user message."""
    from app.container import DreamContainer
    from app.providers.embeddings.fake import FakeEmbeddingProvider
    from app.providers.llm.fake import FakeLLMProvider

    with pytest.raises(ConfigurationError):
        await DreamContainer.create(
            settings.model_copy(update={"prompts_dir": "", "prompts": {}}),
            database=database,
            llm=FakeLLMProvider(),
            embeddings=FakeEmbeddingProvider(),
            ensure_schema=False,
        )


# --------------------------------------------------------------------------- #
# Only what this configuration can reach
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, ("extraction", "decision", "synthesis")),
        (
            {"dream": {"decision_mode": "staged"}},
            ("extraction", "supersede", "merge", "synthesis"),
        ),
        ({"dream": {"synthesis": {"enabled": False}}}, ("extraction", "decision")),
        (
            {"dream": {"compaction": {"enabled": True}}},
            ("extraction", "decision", "synthesis", "compaction"),
        ),
    ],
)
def test_required_prompts_follow_configuration(overrides, expected):
    """Demanding a synthesis prompt from a deployment with synthesis switched
    off would be a startup failure over something that can never run."""
    assert required_prompts(_settings(**overrides)) == expected


def test_the_smallest_working_setup_is_two_files():
    minimal = _settings(dream={"synthesis": {"enabled": False}})
    assert len(required_prompts(minimal)) == 2


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def test_a_directory_of_files_is_the_normal_case():
    library = PromptLibrary.from_settings(_configured())

    assert "Extract facts" in library.get("extraction")
    assert library.describe()["extraction"].endswith("extraction.md")


def test_inline_text_beats_a_file():
    """How one prompt is overridden for an experiment without copying the rest."""
    library = PromptLibrary.from_settings(
        _configured(prompts={"extraction": "Extract only allergies."})
    )

    assert library.get("extraction") == "Extract only allergies."
    assert library.describe()["extraction"] == "configuration"
    assert library.describe()["decision"].endswith("decision.md")


def test_an_unknown_prompt_name_is_refused():
    """A typo would otherwise be a prompt that silently never applies."""
    with pytest.raises(ConfigurationError, match="extration"):
        PromptLibrary.from_settings(_configured(prompts={"extration": "..."}))


def test_a_prompts_dir_that_does_not_exist_says_so(tmp_path):
    with pytest.raises(ConfigurationError, match="not a directory"):
        PromptLibrary.from_settings(_settings(prompts_dir=str(tmp_path / "nope")))


def test_an_empty_file_counts_as_missing(tmp_path):
    """An empty prompt is a misconfiguration that would otherwise reach the
    model as an empty system message."""
    (tmp_path / "extraction.md").write_text("   \n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="extraction"):
        PromptLibrary.from_settings(_settings(prompts_dir=str(tmp_path)))


@pytest.mark.parametrize("suffix", [".md", ".txt", ".prompt"])
def test_any_of_the_accepted_suffixes_works(tmp_path, suffix):
    (tmp_path / f"extraction{suffix}").write_text("Extract things.", encoding="utf-8")
    library = PromptLibrary.from_settings(
        _settings(prompts_dir=str(tmp_path)), validate=False
    )

    assert library.get("extraction") == "Extract things."


def test_the_whole_file_is_the_prompt(tmp_path):
    """No front matter, no comment syntax: whatever is in the file is sent."""
    (tmp_path / "extraction.md").write_text("# Heading\n\nRule one.", encoding="utf-8")
    library = PromptLibrary.from_settings(
        _settings(prompts_dir=str(tmp_path)), validate=False
    )

    assert library.get("extraction") == "# Heading\n\nRule one."


# --------------------------------------------------------------------------- #
# Substitution
# --------------------------------------------------------------------------- #


def test_domain_is_substituted_when_set():
    library = PromptLibrary.from_settings(_configured(domain="A restaurant bot."))
    assert "Context: A restaurant bot." in library.get("extraction")


def test_domain_leaves_nothing_behind_when_unset():
    text = PromptLibrary.from_settings(_configured()).get("extraction")
    assert "{domain}" not in text
    assert "Context:" not in text


def test_braces_in_a_prompt_survive(tmp_path):
    """Prompts carry JSON examples. Formatting them with str.format would either
    raise or mangle the text, so substitution is a plain replacement."""
    body = 'Reply like {"action":"ADD","targetMemoryIds":[]}. {domain}'
    (tmp_path / "extraction.md").write_text(body, encoding="utf-8")

    library = PromptLibrary.from_settings(
        _settings(prompts_dir=str(tmp_path), domain="A bank."), validate=False
    )

    assert '{"action":"ADD","targetMemoryIds":[]}' in library.get("extraction")
    assert "Context: A bank." in library.get("extraction")


def test_asking_for_a_prompt_that_was_never_supplied_explains_itself():
    library = PromptLibrary.from_settings(_settings(), validate=False)

    with pytest.raises(ConfigurationError, match="compaction"):
        library.get("compaction")


def test_the_starter_set_covers_every_prompt_memdream_can_ask_for():
    """Otherwise `python -m memdream prompts` hands someone an incomplete set."""
    shipped = {path.stem for path in TEMPLATES.glob("*.md")} - {"README"}
    assert shipped == set(PROMPT_NAMES)


async def test_ready_reports_which_prompts_are_in_force(container):
    """One deployment behaving unlike another is usually this."""
    checks = await container.health()

    assert checks["prompts"]["ok"] is True
    assert checks["prompts"]["sources"]["extraction"].endswith("extraction.md")


# --------------------------------------------------------------------------- #
# Shorthand
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("shorthand", "expected"),
    [
        ({"llm": "openai:gpt-4o-mini"},
         {"llm_provider": "openai", "openai_chat_model": "gpt-4o-mini"}),
        ({"llm": "azure:gpt-4o"},
         {"llm_provider": "azure_openai", "azure_openai_chat_deployment": "gpt-4o"}),
        ({"llm": "claude:claude-sonnet-4-5"},
         {"llm_provider": "anthropic", "anthropic_model": "claude-sonnet-4-5"}),
        ({"llm": "gemini:gemini-2.0-flash"},
         {"llm_provider": "gemini", "gemini_chat_model": "gemini-2.0-flash"}),
        ({"llm": "groq:llama-3.3-70b"},
         {"llm_provider": "groq", "openai_chat_model": "llama-3.3-70b"}),
        ({"llm": "fake"}, {"llm_provider": "fake"}),
        ({"embeddings": "openai:text-embedding-3-small"},
         {"embedding_provider": "openai", "openai_embedding_model": "text-embedding-3-small"}),
        ({"embeddings": "openai:text-embedding-3-small:512"},
         {"embedding_provider": "openai",
          "openai_embedding_model": "text-embedding-3-small",
          "openai_embedding_dimensions": 512}),
        ({"embeddings": "gemini:gemini-embedding-001:768"},
         {"embedding_provider": "gemini",
          "gemini_embedding_model": "gemini-embedding-001",
          "gemini_embedding_dimensions": 768}),
        ({"vector": "faiss:./data/faiss"},
         {"vector_provider": "faiss", "faiss_path": "./data/faiss"}),
        ({"vector": "memory"}, {"vector_provider": "memory"}),
        ({"vector": "qdrant:http://localhost:6333"},
         {"vector_provider": "qdrant", "qdrant_url": "http://localhost:6333"}),
        ({"vector": "chroma:./data/chroma"},
         {"vector_provider": "chroma", "chroma_path": "./data/chroma"}),
        ({"vector": "chroma:http://chroma.internal:8000"},
         {"vector_provider": "chroma", "chroma_host": "http://chroma.internal:8000"}),
        ({"database": "mongodb://localhost:27017/memdream"},
         {"database_provider": "mongodb",
          "mongodb_uri": "mongodb://localhost:27017/memdream",
          "mongodb_database": "memdream"}),
        ({"database": "postgresql://u:p@host:5432/db"},
         {"database_provider": "postgres", "postgres_dsn": "postgresql://u:p@host:5432/db"}),
        ({"prompts": "./prompts"}, {"prompts_dir": "./prompts"}),
        ({"prompts": {"extraction": "x"}}, {"prompts": {"extraction": "x"}}),
    ],
)
def test_shorthand_expands_to_the_fields_that_provider_reads(shorthand, expected):
    assert expand(shorthand) == expected


def test_expanded_shorthand_is_accepted_by_settings():
    """The expansion is worthless if the field names have drifted."""
    settings = Settings(
        env="test",
        identity_salt="t",
        **expand({"llm": "openai:gpt-4o-mini", "embeddings": "openai:text-embedding-3-small",
                  "vector": "memory", "prompts": str(TEMPLATES)}),
    )

    assert settings.llm_provider == "openai"
    assert settings.openai_chat_model == "gpt-4o-mini"
    assert settings.vector_provider == "memory"
    assert required_prompts(settings)


def test_ordinary_settings_pass_through_untouched():
    assert expand({"log_level": "DEBUG", "llm": "fake"}) == {
        "log_level": "DEBUG", "llm_provider": "fake",
    }


@pytest.mark.parametrize(
    ("shorthand", "match"),
    [
        ({"llm": "nope:x"}, "Unknown LLM provider"),
        ({"embeddings": "anthropic:x"}, "Unknown embedding provider"),
        ({"vector": "elasticsearch:x"}, "Unknown vector store provider"),
        ({"database": "mysql://localhost/db"}, "Unrecognised database"),
        ({"embeddings": "openai:model:big"}, "must be a number"),
    ],
)
def test_a_provider_nobody_has_is_an_error(shorthand, match):
    """Falling back to the default would mean a deployment quietly running on a
    model nobody chose."""
    with pytest.raises(ConfigurationError, match=match):
        expand(shorthand)


def test_anthropic_is_refused_for_embeddings():
    """It has no embeddings API, so the shorthand must not suggest it does."""
    with pytest.raises(ConfigurationError):
        expand({"embeddings": "anthropic:claude-sonnet-4-5"})


# --------------------------------------------------------------------------- #
# Through the SDK
# --------------------------------------------------------------------------- #


async def test_the_sdk_accepts_shorthand(database):
    from memdream import AsyncMemDream

    async with AsyncMemDream(
        user_id="alice",
        database=database,
        llm="fake",
        embeddings="fake",
        vector="memory",
        prompts=str(TEMPLATES),
        identity_salt="t",
    ) as mem:
        settings = mem.container.settings
        assert settings.llm_provider == "fake"
        assert settings.vector_provider == "memory"
        assert settings.prompts_dir == str(TEMPLATES)

        result = await mem.add("I live in Chennai.")
        assert result.action == "ADD"


async def test_a_database_connection_string_is_shorthand_too(container):
    """`database=` takes a connected store or a connection string, and a string
    must not be mistaken for a store."""
    from memdream import AsyncMemDream

    mem = AsyncMemDream(user_id="alice", database="mongodb://localhost:27017/whatever")

    assert mem._database is None
    assert mem._overrides["database"] == "mongodb://localhost:27017/whatever"
