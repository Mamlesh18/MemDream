"""Configuration loading.

These exist because of a real first-run failure: `.env.example` ships
`DREAM_API_KEYS=` (empty), and pydantic-settings JSON-decodes `list[str]` fields
*before* field validators run — so an empty value, and the documented
comma-separated form, both raised `SettingsError` before the service could boot.

The earlier test suite missed it entirely: tests construct `Settings(...)` with
explicit keyword arguments, and no `.env` file exists in a fresh checkout, so the
environment source was never exercised with a real value.
"""

from __future__ import annotations

import textwrap

import pytest

from app.core.config import Settings

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate from the developer's own environment and any `.env` on disk."""
    for name in list(__import__("os").environ):
        if name.startswith("DREAM_") or name.startswith("AZURE_OPENAI"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DREAM_CONFIG_FILE", "")


# --------------------------------------------------------------------------- #
# List fields from the environment
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),                                   # what .env.example ships
        ("   ", []),
        ("key1", ["key1"]),
        ("key1,key2", ["key1", "key2"]),
        ("  key1 , key2  ", ["key1", "key2"]),      # tolerant of spacing
        ("key1,,key2,", ["key1", "key2"]),          # tolerant of stray commas
        ('["key1","key2"]', ["key1", "key2"]),      # JSON form still works
        ("[]", []),
    ],
)
async def test_api_keys_parse_from_the_environment(monkeypatch, raw, expected):
    monkeypatch.setenv("DREAM_API_KEYS", raw)
    assert Settings().api_keys == expected


async def test_empty_api_keys_disables_auth(monkeypatch):
    monkeypatch.setenv("DREAM_API_KEYS", "")
    assert Settings().auth_enabled is False


async def test_present_api_keys_enable_auth(monkeypatch):
    monkeypatch.setenv("DREAM_API_KEYS", "secret")
    assert Settings().auth_enabled is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("event,other", ["event", "other"]),
        ('["event"]', ["event"]),
        ("", []),
    ],
)
async def test_nested_list_fields_parse_from_the_environment(monkeypatch, raw, expected):
    """A nested list must not fail the whole section it lives in."""
    monkeypatch.setenv("DREAM_MEMORY__BLOCKED_MEMORY_TYPES", raw)
    assert Settings().memory.blocked_memory_types == expected


async def test_dict_fields_still_accept_json(monkeypatch):
    monkeypatch.setenv(
        "DREAM_RETRIEVAL__STATUS_MULTIPLIERS", '{"active": 1.0, "superseded": 0.9}'
    )
    assert Settings().retrieval.status_multipliers["superseded"] == 0.9


# --------------------------------------------------------------------------- #
# The whole `.env.example` must boot
# --------------------------------------------------------------------------- #


async def test_the_shipped_env_example_loads(monkeypatch, tmp_path):
    """`cp .env.example .env` is step one of the README. It has to work.

    Parsed and applied as environment variables rather than by pointing
    `env_file` at it, so the test does not depend on the process's working
    directory.
    """
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / ".env.example"
    assert example.is_file(), ".env.example is part of the documented setup"

    for line in example.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        monkeypatch.setenv(key.strip(), value.strip())

    settings = Settings()
    assert settings.api_keys == []
    assert settings.mongodb_database == "memdream"
    assert settings.azure_openai_embedding_deployment == "text-embedding-3-small"


# --------------------------------------------------------------------------- #
# Precedence
# --------------------------------------------------------------------------- #


async def test_scalar_nested_overrides_work(monkeypatch):
    """The form documented in the README."""
    monkeypatch.setenv("DREAM_RETRIEVAL__TOP_K", "20")
    monkeypatch.setenv("DREAM_DREAM__SYNTHESIS__MIN_MEMORIES", "50")

    settings = Settings()
    assert settings.retrieval.top_k == 20
    assert settings.dream.synthesis.min_memories == 50


async def test_yaml_overlay_is_applied(monkeypatch, tmp_path):
    config = tmp_path / "dream.yaml"
    config.write_text(
        textwrap.dedent(
            """
            retrieval:
              top_k: 33
              similarity_threshold: 0.42
            memory:
              min_confidence: 0.75
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DREAM_CONFIG_FILE", str(config))

    settings = Settings()
    assert settings.retrieval.top_k == 33
    assert settings.retrieval.similarity_threshold == 0.42
    assert settings.memory.min_confidence == 0.75


async def test_environment_beats_yaml(monkeypatch, tmp_path):
    config = tmp_path / "dream.yaml"
    config.write_text("retrieval:\n  top_k: 33\n", encoding="utf-8")
    monkeypatch.setenv("DREAM_CONFIG_FILE", str(config))
    monkeypatch.setenv("DREAM_RETRIEVAL__TOP_K", "77")

    assert Settings().retrieval.top_k == 77


async def test_a_missing_yaml_file_is_not_fatal(monkeypatch):
    """Defaults plus environment are enough to boot."""
    monkeypatch.setenv("DREAM_CONFIG_FILE", "/nonexistent/dream.yaml")
    assert Settings().retrieval.top_k == 10


async def test_the_shipped_yaml_config_loads(monkeypatch):
    from pathlib import Path

    config = Path(__file__).resolve().parents[1] / "config" / "dream.yaml"
    assert config.is_file()
    monkeypatch.setenv("DREAM_CONFIG_FILE", str(config))

    settings = Settings()
    # Values measured by scripts/calibrate.py against text-embedding-3-small.
    assert settings.retrieval.similarity_threshold == 0.30
    assert settings.dream.supersede.similarity_threshold == 0.40
    assert settings.dream.merge.similarity_threshold == 0.40
    assert settings.dream.synthesis.cluster_similarity_threshold == 0.45
    assert settings.retrieval.status_multipliers["superseded"] == 0.45


async def test_shipped_yaml_and_code_defaults_agree(monkeypatch):
    """The YAML overlay documents the defaults; drift between them is a trap.

    An operator reading config/dream.yaml would otherwise be told one value
    while a deployment that does not set DREAM_CONFIG_FILE silently uses another.
    """
    from pathlib import Path

    code_defaults = Settings()

    config = Path(__file__).resolve().parents[1] / "config" / "dream.yaml"
    monkeypatch.setenv("DREAM_CONFIG_FILE", str(config))
    from_yaml = Settings()

    for section, field in [
        ("retrieval", "similarity_threshold"),
        ("retrieval", "top_k"),
        ("retrieval", "recency_half_life_days"),
    ]:
        assert getattr(getattr(code_defaults, section), field) == getattr(
            getattr(from_yaml, section), field
        ), f"{section}.{field} differs between code defaults and dream.yaml"

    assert (
        code_defaults.dream.supersede.similarity_threshold
        == from_yaml.dream.supersede.similarity_threshold
    )
    assert (
        code_defaults.dream.merge.similarity_threshold
        == from_yaml.dream.merge.similarity_threshold
    )
    assert (
        code_defaults.dream.synthesis.cluster_similarity_threshold
        == from_yaml.dream.synthesis.cluster_similarity_threshold
    )
    assert code_defaults.memory.min_confidence == from_yaml.memory.min_confidence


# --------------------------------------------------------------------------- #
# Fail-fast on missing credentials
# --------------------------------------------------------------------------- #


async def test_azure_credentials_are_checked_with_an_actionable_message():
    settings = Settings(llm_provider="azure_openai", azure_openai_endpoint="", azure_openai_api_key="")

    with pytest.raises(ValueError) as exc:
        settings.require_azure_openai()

    message = str(exc.value)
    assert "AZURE_OPENAI_ENDPOINT" in message
    assert "AZURE_OPENAI_API_KEY" in message
    # Points at the offline escape hatch rather than just complaining.
    assert "fake" in message


async def test_azure_credentials_present_passes():
    settings = Settings(
        azure_openai_endpoint="https://example.openai.azure.com/",
        azure_openai_api_key="secret",
    )
    settings.require_azure_openai()
