"""Configuration.

Precedence (highest first):

    1. explicit keyword arguments
    2. environment variables      (``DREAM_RETRIEVAL__TOP_K=20``)
    3. ``.env``
    4. the YAML file named by ``DREAM_CONFIG_FILE``
    5. the defaults declared here

Nothing that influences Dream behaviour is hard-coded at a call site: every
threshold, weight and toggle lives on one of the models below so that operators
can retune the system without touching Python.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import AliasChoices, BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


def _parse_string_list(value: Any) -> Any:
    """Accept a comma-separated string, a JSON array, or an actual list.

    Environment variables are strings, and an operator writing
    ``DREAM_API_KEYS=key1,key2`` is doing the obvious thing. Without this,
    pydantic-settings JSON-decodes every ``list[str]`` field before validation
    runs, so both the comma form *and* an empty value raise a parse error — the
    empty case being what `.env.example` ships, i.e. every first run.
    """
    if not isinstance(value, str):
        return value

    text = value.strip()
    if not text:
        return []
    # Still honour the JSON form, so an existing '["a","b"]' keeps working.
    if text.startswith("["):
        import json

        try:
            parsed = json.loads(text)
        except ValueError:
            pass
        else:
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in text.split(",") if item.strip()]

# --------------------------------------------------------------------------- #
# Tuning sections
# --------------------------------------------------------------------------- #


class RetrievalWeights(BaseModel):
    """Weights for the hybrid ranking formula (see ``app.memory.ranker``).

    They do not have to sum to 1.0 — the ranker normalises them — but keeping
    them close to 1.0 makes the resulting scores easier to read in logs.
    """

    semantic: float = 0.60
    importance: float = 0.15
    confidence: float = 0.10
    recency: float = 0.10
    status: float = 0.05


class RetrievalSettings(BaseModel):
    top_k: int = 10
    #: How many rows to pull out of the vector index before hybrid re-ranking.
    candidate_multiplier: int = 4
    #: Noise floor: vector hits below this cosine similarity never reach the
    #: ranker.
    #:
    #: Measured against `text-embedding-3-small` on short third-person facts
    #: (`python -m scripts.calibrate`, 27 labelled pairs):
    #:
    #:     unrelated       0.16 - 0.36
    #:     duplicate       0.46 - 0.87
    #:     contradiction   0.57 - 0.91
    #:     near-identical  0.94 - 1.00
    #:
    #: Note how high the floor sits: *unrelated* text scores up to 0.36 with this
    #: model, so a threshold that looks generous can still be below the noise.
    #: A threshold up at 0.75 — intuitive if you read cosine as a percentage —
    #: would sit above most real duplicates and quietly disable the Dream layer.
    #: Re-measure with scripts/calibrate.py if you change embedding model; the
    #: bands move a lot between models.
    similarity_threshold: float = 0.30
    #: Half-life in days for the recency component of the hybrid score.
    recency_half_life_days: float = 90.0
    weights: RetrievalWeights = Field(default_factory=RetrievalWeights)
    #: The status component of the weighted sum above.
    status_scores: dict[str, float] = Field(
        default_factory=lambda: {
            "active": 1.0,
            "superseded": 0.4,
            "merged": 0.3,
            "expired": 0.1,
        }
    )
    #: Multiplicative demotion applied to the final score, by status.
    #:
    #: This exists in addition to `status_scores` because the additive term
    #: cannot do the job on its own. At the specified weight of 0.05, the gap
    #: between active and superseded is worth 0.03 of final score — easily
    #: outweighed by a superseded memory that happens to phrase things closer to
    #: the query. That is the exact failure that matters: asking "where does the
    #: user live?" and getting the city they left. A superseded fact is not
    #: slightly worse evidence, it is *false*, and it should surface only when
    #: history was asked for. Set every entry to 1.0 to get the pure additive
    #: formula back.
    status_multipliers: dict[str, float] = Field(
        default_factory=lambda: {
            "active": 1.0,
            "superseded": 0.45,
            "merged": 0.40,
            "expired": 0.25,
        }
    )


class SupersedeSettings(BaseModel):
    enabled: bool = True
    #: Floor for offering an existing memory to the supersede reasoner.
    #:
    #: Measured contradictions run 0.57-0.91 with `text-embedding-3-small`,
    #: because the two memories share their *predicate* — "lives in Chennai" and
    #: "lives in Bangalore" differ in one token and score 0.77. This sits well
    #: below the weakest measured case so an unusually-phrased replacement still
    #: reaches the reasoner.
    similarity_threshold: float = 0.40
    max_candidates: int = 10


class MergeSettings(BaseModel):
    enabled: bool = True
    #: Duplicates measure 0.46-0.87. Counter-intuitively this is *lower* than
    #: the contradiction band, because a paraphrase may share no vocabulary
    #: ("likes black coffee" / "prefers coffee without milk" scores 0.46) while a
    #: contradiction differs by a single word. So merge cannot simply use a
    #: higher bar than supersede — the two bands overlap, and separating them is
    #: the reasoner's job, not the threshold's.
    similarity_threshold: float = 0.40
    max_candidates: int = 10
    #: Identical normalised content short-circuits to NOOP without an LLM call.
    exact_duplicate_shortcut: bool = True


class CandidateSettings(BaseModel):
    """How existing memories are gathered for a Dream decision.

    Semantic similarity alone is not enough. The clearest supersede cases are
    exactly the ones where the old and new text share almost no words, so the
    candidate set is the *union* of two channels: vector neighbours, and the
    most recent active memories of the same type. The second channel is cheap
    (one indexed Mongo query) and catches what embeddings miss.
    """

    #: Recent same-type active memories always offered alongside vector hits.
    same_type_recent: int = 5
    #: Hard cap on the union, to bound prompt size and cost.
    max_total: int = 12


class SynthesisSettings(BaseModel):
    enabled: bool = True
    #: A user needs at least this many active memories before synthesis runs.
    min_memories: int = 20
    #: A cluster needs at least this many members to become a pattern.
    min_cluster_size: int = 3
    max_cluster_size: int = 12
    #: Agglomerative clustering cut-off on cosine similarity. Above the measured
    #: unrelated ceiling (0.36) so coincidence does not form clusters, but below
    #: the duplicate band so genuinely related memories still group.
    cluster_similarity_threshold: float = 0.45
    #: Most clusters sent to the LLM in a single job.
    max_clusters_per_run: int = 20
    #: Candidate pool size handed to the clusterer.
    max_candidates: int = 200
    #: A proposed pattern this similar to an existing one is treated as the same
    #: pattern (updated in place) rather than a new one. High by design: this is
    #: the guard against pattern spam, and two patterns that really do say the
    #: same thing land in the measured near-identical band (0.94+) or the top of
    #: the duplicate band.
    duplicate_similarity_threshold: float = 0.82
    #: ...or if this fraction of its sources overlap an existing pattern's.
    duplicate_source_overlap: float = 0.60
    #: Patterns below these bars are discarded rather than persisted.
    min_pattern_confidence: float = 0.55
    min_pattern_importance: float = 0.40
    # --- automatic triggering ------------------------------------------- #
    #
    # Instead of sweeping every user on a timer — which wakes up for people who
    # have added nothing — synthesis fires when a user has actually accumulated
    # enough new material. Both inputs are already durable in Mongo, so nothing
    # needs a counter that could drift.

    auto_trigger: bool = True
    #: New active memories since the last completed run before synthesis fires.
    auto_trigger_new_memories: int = 20
    #: Minimum gap between runs for one user. Without this a chatty user would
    #: trigger synthesis every few minutes and spend tokens re-deriving the
    #: same patterns.
    auto_trigger_cooldown_seconds: int = 21600  # 6 hours
    #: Ceiling on synthesis jobs in flight across all users. A traffic spike
    #: must not turn into a rate-limit incident on the provider.
    auto_trigger_max_concurrent: int = 2
    #: A job with no progress for this long is treated as abandoned, so a dead
    #: worker cannot block a user's synthesis forever.
    job_stale_after_seconds: int = 900

    # --- the in-process pool (DREAM_QUEUE_PROVIDER=inline) --------------- #

    #: Synthesis jobs this process runs at once. Two is deliberate: the work is
    #: LLM-bound, so the ceiling that matters is the provider's rate limit, not
    #: the CPU. Distinct from `auto_trigger_max_concurrent`, which is a
    #: deployment-wide gate read from the database and therefore also counts
    #: jobs running on other replicas.
    max_concurrent: int = 2
    #: Jobs allowed to wait. Beyond this, new ones are failed immediately with a
    #: reason rather than queued — a row sitting at `queued` reads as in-flight
    #: and would block that user's next trigger for the whole stale timeout.
    #: Since the trigger is derived from durable state, the next write re-queues.
    max_queued: int = 100
    #: How long shutdown waits for in-flight synthesis before abandoning it.
    #: Long enough for a job that is nearly done, short enough not to hold a
    #: deploy open.
    drain_seconds: float = 30.0


class ConsolidationSettings(BaseModel):
    """Retiring memories that no longer earn their place.

    Forgetting here means a status transition to `expired`, never a delete: the
    row leaves the search index and stays auditable. That is what makes it safe
    to act on a heuristic — being wrong costs a flip, not a row.
    """

    enabled: bool = True
    #: Ceiling on memories examined in one pass, so a very large account cannot
    #: turn a nightly sweep into an unbounded scan.
    max_per_run: int = 500

    # --- the nightly clock ---------------------------------------------- #

    schedule_enabled: bool = True
    #: Hour of the day, UTC. Default is midnight UTC; move it to your own quiet
    #: hour rather than everyone else's.
    schedule_hour_utc: int = Field(default=0, ge=0, le=23)

    # --- recall decay ---------------------------------------------------- #
    #
    # **Off by default, and it must stay off until you have watched it.**
    #
    # Expiring a memory because nothing has recalled it is a judgement, not a
    # fact — unlike a closed validity window, which is provably false. Judgement
    # needs evidence, and the evidence is `lastRecalledAt`, which only starts
    # accumulating the day recall tracking is switched on.
    #
    # Leave `decay_enabled` false and read `consolidation_would_expire` in the
    # logs for a few weeks. Set the thresholds from what you see, the way the
    # similarity bands were measured. Then turn it on.

    decay_enabled: bool = False
    #: Not recalled within this many days is the *candidate* condition. Every
    #: guard below still has to pass.
    decay_after_days: int = 90
    #: Importance at or above this is never decayed, however long it sits
    #: untouched. An allergy nobody has asked about in a year still outranks
    #: yesterday's lunch order, and this is the line that says so.
    decay_importance_floor: float = Field(default=0.5, ge=0.0, le=1.0)
    #: When recall tracking began, as an ISO date. **The cold-start guard.**
    #:
    #: Every memory written before tracking existed has `lastRecalledAt: null`,
    #: which is indistinguishable from "never used" — so on the day you enable
    #: decay, the entire database looks maximally stale and would be expired in
    #: one pass. Setting this makes a memory eligible only once the decay window
    #: has elapsed *since tracking started*, giving every row a fair chance to
    #: be recalled before it is judged.
    #:
    #: Empty disables recall decay outright, whatever `decay_enabled` says.
    recall_epoch: str = ""


class DreamSettings(BaseModel):
    #: ``unified``  — one LLM call decides ADD / SUPERSEDE / MERGE / NOOP.
    #: ``staged``   — a merge-focused call, then a supersede-focused call, each
    #:               using its dedicated prompt. More accurate on hard cases and
    #:               easier to debug; roughly twice the tokens.
    decision_mode: Literal["unified", "staged"] = "unified"
    #: Number of related memories shown to the reasoner for one candidate.
    max_related_candidates: int = 10

    candidates: CandidateSettings = Field(default_factory=CandidateSettings)
    supersede: SupersedeSettings = Field(default_factory=SupersedeSettings)
    merge: MergeSettings = Field(default_factory=MergeSettings)
    synthesis: SynthesisSettings = Field(default_factory=SynthesisSettings)
    consolidation: ConsolidationSettings = Field(default_factory=ConsolidationSettings)


class MemoryPolicySettings(BaseModel):
    """Guard rails applied to every candidate memory before it is persisted."""

    min_confidence: float = 0.60
    min_importance: float = 0.20
    #: Empty list means "all types allowed".
    #: `NoDecode` + the validator accept ``DREAM_MEMORY__BLOCKED_MEMORY_TYPES=event,other``
    #: as well as a JSON array. Without it the whole `memory` section fails to
    #: parse with a message that names the section rather than the field.
    allowed_memory_types: Annotated[list[str], NoDecode] = Field(default_factory=list)
    blocked_memory_types: Annotated[list[str], NoDecode] = Field(default_factory=list)

    _parse_lists = field_validator(
        "allowed_memory_types", "blocked_memory_types", mode="before"
    )(_parse_string_list)
    #: Drop candidates the extractor flagged as sensitive (PII, credentials,
    #: health, financial) unless the deployment explicitly opts in.
    allow_sensitive: bool = False
    max_content_chars: int = 1000
    #: Most candidate memories accepted from a single prompt.
    max_candidates_per_prompt: int = 8


class LLMSettings(BaseModel):
    temperature: float = 0.1
    max_output_tokens: int = 1200
    timeout_seconds: float = 60.0
    max_retries: int = 3


class PricingSettings(BaseModel):
    """Rates used to turn token counts into cost estimates.

    Per *million* tokens, which is how providers quote them. Defaults are the
    published Azure OpenAI rates for `gpt-4o-mini` and `text-embedding-3-small`
    at the time of writing.

    These are **estimates from configured numbers, never billed amounts**. Rates
    change, vary by region and commitment, and a deployment may not use these
    models at all. Set them to match your own contract, or to 0 to disable cost
    reporting entirely.
    """

    llm_input_per_1m: float = 0.15
    llm_output_per_1m: float = 0.60
    embedding_per_1m: float = 0.02
    currency: str = "USD"

    def cost_for(
        self, *, input_tokens: int = 0, output_tokens: int = 0, embedding_tokens: int = 0
    ) -> float:
        return (
            input_tokens * self.llm_input_per_1m
            + output_tokens * self.llm_output_per_1m
            + embedding_tokens * self.embedding_per_1m
        ) / 1_000_000


class EmbeddingSettings(BaseModel):
    batch_size: int = 64
    timeout_seconds: float = 30.0
    max_retries: int = 3
    #: Embedding inputs longer than this are truncated (defensive; memories are
    #: short by construction).
    max_input_chars: int = 8000


# --------------------------------------------------------------------------- #
# YAML source
# --------------------------------------------------------------------------- #


class _YamlSettingsSource(PydanticBaseSettingsSource):
    """Loads the optional YAML overlay named by ``DREAM_CONFIG_FILE``."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._data: dict[str, Any] = {}
        path = os.environ.get("DREAM_CONFIG_FILE", "").strip()
        if not path:
            return
        file = Path(path)
        if not file.is_file():
            # A missing overlay is not an error: defaults + env are enough to boot.
            return
        loaded = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            self._data = loaded

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:  # noqa: D102
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data


# --------------------------------------------------------------------------- #
# Root settings
# --------------------------------------------------------------------------- #


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DREAM_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
        # So `Settings(azure_openai_endpoint=...)` still works alongside the
        # validation aliases above.
        populate_by_name=True,
    )

    # --- service ---------------------------------------------------------- #
    env: str = "local"
    service_name: str = "dream-memory"
    log_level: str = "INFO"
    log_json: bool = True
    #: Unbound shared secrets. These authenticate but carry no tenant, so
    #: `clientId` still comes from the request — right for single-tenant.
    #: NoDecode stops pydantic-settings from JSON-decoding this before the
    #: validator below runs. Comma-separated is the documented form.
    api_keys: Annotated[list[str], NoDecode] = Field(default_factory=list)

    #: Keys bound to a tenant: ``key1:acme,key2:globex``. For a bound key the
    #: tenant comes from the credential and a request naming a different one is
    #: refused. Required for any multi-tenant deployment.
    api_key_clients: Annotated[list[str], NoDecode] = Field(default_factory=list)

    #: Refuse unbound keys outright, so a leftover shared secret cannot be used
    #: to reach another tenant. Turn this on when hosting.
    api_key_require_binding: bool = False

    # --- storage ---------------------------------------------------------- #
    #
    # The canonical store: memories, edges, audit rows and jobs. `mongodb` is
    # the default because MemDream was designed against a document store — an
    # open memory-type vocabulary and nested metadata need no migration there.
    # `postgres` exists because most teams already run one, and pairing it with
    # the pgvector store means MemDream needs exactly one piece of
    # infrastructure. Behaviour is identical either way, and
    # tests/test_repository_conformance.py is what makes that a fact rather
    # than a claim.
    database_provider: Literal["mongodb", "postgres"] = "mongodb"

    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_database: str = "memdream"

    #: Connections held open per process. The API and the worker each hold their
    #: own pool, so budget for both against the server's max_connections.
    postgres_pool_size: int = 10

    #: Where background synthesis runs. Note that *nothing on the user's path*
    #: goes through here: remember, recall, supersede and merge are all
    #: synchronous inside the API. This governs synthesis only.
    #:
    #: ``inline``   — a bounded pool of tasks in the API process. **Default**,
    #:                because it is one service, no broker, nothing to deploy.
    #: ``database`` — the jobs table is the queue, claimed by a separate worker
    #:                process. For several API replicas sharing one queue, or
    #:                when a job must survive a restart. ``mongo`` is the
    #:                previous spelling.
    queue_provider: Literal["inline", "embedded", "database", "mongo"] = "inline"

    vector_provider: Literal[
        "faiss", "memory", "qdrant", "pgvector", "chroma", "pinecone", "milvus",
        "azure_search",
    ] = "faiss"
    faiss_path: str = "./data/faiss"
    faiss_max_loaded_indexes: int = 128

    # --- vector backends (only the selected one is read) ------------------- #
    #
    # Isolation differs per backend and is asserted for all of them by
    # tests/test_vector_store_conformance.py:
    #   pinecone            namespace per tenant   (hard partition)
    #   qdrant/chroma/…     indexed scope filter   (soft)
    #   pgvector            composite primary key  (enforced by the schema)

    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "memdream"

    #: postgresql://user:pass@host:5432/db
    #:
    #: Shared by the `postgres` canonical store and the `pgvector` vector store.
    #: Pointing both at the same database is the intended setup: one server, one
    #: backup, and memories and their vectors in the same transaction domain.
    postgres_dsn: str = ""
    pgvector_table: str = "memdream_vectors"

    #: Local persistence directory; set chroma_host instead for client/server.
    chroma_path: str = "./data/chroma"
    chroma_host: str = ""
    chroma_port: int = 8000
    chroma_collection: str = "memdream"

    pinecone_api_key: str = ""
    pinecone_index: str = "memdream"
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"

    #: An http(s) URL, or a local file path to use Milvus Lite with no server.
    milvus_uri: str = "http://localhost:19530"
    milvus_token: str = ""
    milvus_collection: str = "memdream"

    azure_search_endpoint: str = ""
    azure_search_api_key: str = ""
    azure_search_index: str = "memdream"

    # --- providers -------------------------------------------------------- #
    #
    # LLM and embeddings are chosen independently on purpose: several services
    # do chat but not embeddings (Groq, Anthropic), so "Groq for chat, OpenAI
    # for embeddings" is a normal and common pairing.
    llm_provider: Literal[
        "azure_openai", "openai", "groq", "ollama", "together", "fireworks",
        "openai_compatible", "gemini", "anthropic", "fake",
    ] = "azure_openai"
    embedding_provider: Literal[
        "azure_openai", "openai", "ollama", "openai_compatible", "gemini", "fake"
    ] = "azure_openai"

    # Read from the *unprefixed* `AZURE_OPENAI_*` names — the conventional
    # spelling, and what every Azure tutorial and `.env.example` uses. Without
    # an explicit alias, `env_prefix="DREAM_"` would silently look for
    # `DREAM_AZURE_OPENAI_ENDPOINT` and leave these empty, producing a
    # "credentials not set" error while the credentials sit right there in .env.
    # The prefixed form is still accepted, for deployments that namespace
    # everything.
    #
    # The field's own name is listed *first* in every AliasChoices below, and
    # that ordering is load-bearing. Sources are merged by key: the init source
    # contributes `azure_openai_api_key` while the dotenv source contributes
    # `AZURE_OPENAI_API_KEY`, so the two never collide and source precedence
    # never gets to apply — alias order decides instead. Listing the field name
    # first restores the documented behaviour, where an explicit
    # `Settings(azure_openai_api_key=...)` beats whatever is in `.env`. Any new
    # aliased field must follow the same rule.
    azure_openai_endpoint: str = Field(
        default="",
        validation_alias=AliasChoices(
            "azure_openai_endpoint", "AZURE_OPENAI_ENDPOINT", "DREAM_AZURE_OPENAI_ENDPOINT"
        ),
    )
    azure_openai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "azure_openai_api_key", "AZURE_OPENAI_API_KEY", "DREAM_AZURE_OPENAI_API_KEY"
        ),
    )
    azure_openai_api_version: str = Field(
        default="2024-10-21",
        validation_alias=AliasChoices(
            "azure_openai_api_version",
            "AZURE_OPENAI_API_VERSION",
            "DREAM_AZURE_OPENAI_API_VERSION",
        ),
    )
    azure_openai_chat_deployment: str = Field(
        default="gpt-4o-mini",
        validation_alias=AliasChoices(
            "azure_openai_chat_deployment",
            "AZURE_OPENAI_CHAT_DEPLOYMENT",
            "DREAM_AZURE_OPENAI_CHAT_DEPLOYMENT",
        ),
    )
    azure_openai_embedding_deployment: str = Field(
        default="text-embedding-3-small",
        validation_alias=AliasChoices(
            "azure_openai_embedding_deployment",
            "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
            "DREAM_AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
        ),
    )
    azure_openai_embedding_dimensions: int = Field(
        default=1536,
        validation_alias=AliasChoices(
            "azure_openai_embedding_dimensions",
            "AZURE_OPENAI_EMBEDDING_DIMENSIONS",
            "DREAM_AZURE_OPENAI_EMBEDDING_DIMENSIONS",
        ),
    )

    # --- OpenAI-compatible endpoints -------------------------------------- #
    #
    # Shared by openai / groq / ollama / together / fireworks. Leave blank to
    # accept the preset for the chosen provider; set to override. The
    # unprefixed OPENAI_API_KEY is also read, since that is what everyone
    # already has exported.
    openai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "openai_api_key", "DREAM_OPENAI_API_KEY", "OPENAI_API_KEY"
        ),
    )
    openai_base_url: str = ""
    openai_chat_model: str = ""
    openai_embedding_model: str = ""
    #: 0 means "use the preset default for this provider".
    openai_embedding_dimensions: int = 0

    # --- Gemini ------------------------------------------------------------ #
    #
    # Gemini has an OpenAI-compatible shim, but the native SDK is used instead:
    # it guarantees JSON output as a decoding constraint and reports token usage
    # accurately, neither of which the shim does reliably. Gemini does both chat
    # and embeddings, so it can serve as the only provider a deployment needs.
    gemini_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "gemini_api_key", "DREAM_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"
        ),
    )
    gemini_chat_model: str = "gemini-2.0-flash"
    gemini_embedding_model: str = "gemini-embedding-001"
    #: Matryoshka output width. 768 rather than the maximum because a memory
    #: layer runs a similarity search on every write, and the accuracy cost at
    #: this text length is small.
    gemini_embedding_dimensions: int = 768

    # --- Anthropic --------------------------------------------------------- #
    #
    # Chat only: Anthropic has no embeddings API, so pair this with OpenAI,
    # Azure, Gemini or Ollama for `DREAM_EMBEDDING_PROVIDER`.
    anthropic_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "anthropic_api_key", "DREAM_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"
        ),
    )
    anthropic_model: str = "claude-sonnet-4-5"

    #: Record which memories each search returned, so consolidation can later
    #: tell what is still earning its place from what nothing has needed in
    #: months. Costs one indexed bulk update per search.
    #:
    #: It collects only — nothing reads it yet, and nothing will until it has
    #: been running long enough for the signal to mean something. Decay
    #: thresholds set against a week of data would be guesses.
    track_recall: bool = True

    #: Skip the extraction call for messages that provably cannot contain a
    #: memory — greetings, acknowledgements, bare questions. Typically a third
    #: to a half of chat turns, each costing ~660 prompt tokens.
    #:
    #: Deliberately conservative: it fires only on a closed lexicon, never on
    #: length, because "no onions" is nine characters and is a real memory.
    #: Turn it off if you would rather pay than ever risk a missed fact.
    triage_enabled: bool = True

    # --- domain ------------------------------------------------------------ #
    #
    # One sentence describing what this deployment is, injected into the
    # extraction and decision prompts. Empty by default.
    #
    # This is the cheapest accuracy win available. The prompts are deliberately
    # generic and short; a single line of context tells the model that "no
    # onions" is a standing constraint rather than small talk, which no amount
    # of generic rule-writing conveys as well. It costs ~30 tokens per call.
    #
    #     DREAM_DOMAIN="A restaurant ordering assistant. Remember contact and
    #     delivery details, dietary needs, dislikes, and usual orders per meal."
    domain: str = Field(default="", max_length=400)

    # --- privacy ---------------------------------------------------------- #
    identity_salt: str = "dream-memory-default-salt"

    # --- observability ---------------------------------------------------- #
    otel_enabled: bool = False

    # --- tuning ----------------------------------------------------------- #
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    dream: DreamSettings = Field(default_factory=DreamSettings)
    memory: MemoryPolicySettings = Field(default_factory=MemoryPolicySettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    pricing: PricingSettings = Field(default_factory=PricingSettings)

    _split_api_keys = field_validator(
        "api_keys", "api_key_clients", mode="before"
    )(_parse_string_list)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlSettingsSource(settings_cls),
            file_secret_settings,
        )

    # --- derived ---------------------------------------------------------- #

    @property
    def auth_enabled(self) -> bool:
        """True when any credential is configured.

        Empty means no authentication — appropriate behind a private gateway,
        and the reason it is opt-in rather than mandatory.
        """
        return bool(self.api_keys or self.api_key_clients)

    def require_azure_openai(self) -> None:
        """Fail fast with an actionable message rather than at the first call."""
        missing = [
            name
            for name, value in (
                ("AZURE_OPENAI_ENDPOINT", self.azure_openai_endpoint),
                ("AZURE_OPENAI_API_KEY", self.azure_openai_api_key),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"Azure OpenAI is selected but {', '.join(missing)} is not set. "
                "Set them in .env, or switch DREAM_LLM_PROVIDER / "
                "DREAM_EMBEDDING_PROVIDER to 'fake' for offline development."
            )


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the singleton — used by tests that patch the environment."""
    get_settings.cache_clear()
