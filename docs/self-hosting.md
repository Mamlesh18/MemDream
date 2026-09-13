# Self-hosting

Run MemDream as a service your agents call over HTTP — from the [REST API](api.md)
or the [Python SDK](sdk.md)'s `DreamClient`.

A deployment is **one process and one database.** There is no broker, no worker
and no cron container: remembering, recall and consolidation run synchronously
inside the API, and background synthesis runs in a bounded task pool in the same
process.

```text
   your agents ──HTTP──►  MemDream API  ──►  MongoDB           (memories, audit, jobs)
                          (uvicorn)     ──►  vector index      (FAISS on disk, or a service)
                                        ──►  LLM + embeddings  (your provider)
```

---

## Contents

- [Requirements](#requirements)
- [Run with Docker Compose](#run-with-docker-compose)
- [Run directly](#run-directly)
- [Check it is working](#check-it-is-working)
- [Configuration](#configuration)
  - [Essentials](#essentials)
  - [Database](#database)
  - [LLM and embeddings](#llm-and-embeddings)
  - [Vector store](#vector-store)
  - [Authentication](#authentication)
  - [Background synthesis](#background-synthesis)
  - [Consolidation](#consolidation)
  - [Observability](#observability)
- [Scaling](#scaling)
- [Production checklist](#production-checklist)
- [Maintenance scripts](#maintenance-scripts)

---

## Requirements

- **Python 3.10+**, or Docker
- **A database:** MongoDB 6 or newer. PostgreSQL is also supported — see
  [Database](#database)
- **An LLM and an embedding provider:** Azure OpenAI, OpenAI, Gemini, Anthropic
  (chat only), Groq (chat only), Ollama, Together, Fireworks, or any
  OpenAI-compatible endpoint

---

## Run with Docker Compose

The fastest route: the API and MongoDB, with the vector index on a volume.

```bash
git clone https://github.com/Mamlesh18/MemDream.git
cd MemDream
cp .env.example .env          # add your provider credentials — see Configuration
docker compose up --build
```

The API listens on `http://localhost:8000`, with interactive docs at
`http://localhost:8000/docs`. MongoDB is exposed on `localhost:27017`.

---

## Run directly

```bash
git clone https://github.com/Mamlesh18/MemDream.git
cd MemDream

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install .                      # or: pip install ".[gemini,anthropic]"

cp .env.example .env               # set DREAM_MONGODB_URI and provider credentials
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Optional extras: `gemini`, `anthropic`, `postgres`, `qdrant`, `pgvector`,
`chroma`, `pinecone`, `milvus`, `azure-search`, `otel`.

---

## Check it is working

```bash
curl http://localhost:8000/ready
```

Every dependency should report `"ok": true`. Then remember and recall something:

```bash
curl -X POST http://localhost:8000/v1/memories \
  -H "Content-Type: application/json" \
  -d '{"userId": "smoke_test", "prompt": "I live in Chennai."}'

curl -X POST http://localhost:8000/v1/memories/search \
  -H "Content-Type: application/json" \
  -d '{"userId": "smoke_test", "query": "where does the user live?"}'

curl -X DELETE "http://localhost:8000/v1/memories?userId=smoke_test&confirm=ERASE"
```

For a visual view, open `console.html` from the repository in a browser while the
API is running: live decisions, a memory inspector, the audit trail, token costs,
and consolidation dry runs.

---

## Configuration

MemDream is configured with environment variables, read from the process
environment or a `.env` file in the working directory. `.env.example` lists every
common setting with an explanation.

- Variables are prefixed `DREAM_`.
- Nested settings use a double underscore: `DREAM_DREAM__SYNTHESIS__MIN_MEMORIES=30`.
- Fine-grained tuning — thresholds, ranking weights, prices — can also live in a
  YAML file set by `DREAM_CONFIG_FILE` (default `config/dream.yaml`, annotated).
- Well-known provider variables such as `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and
  `GOOGLE_API_KEY` are read without the prefix.

### Essentials

| Variable | Default | |
|---|---|---|
| `DREAM_IDENTITY_SALT` | *(insecure default)* | **Set this.** Salt for deriving user ids from phone numbers. Changing it later changes every derived id, so pick a long random value once and keep it |
| `DREAM_DOMAIN` | empty | one sentence describing your deployment, injected into extraction and decisions. See [describing your domain](concepts.md#describing-your-domain) |
| `DREAM_ENV` | `local` | free-form environment label |
| `DREAM_LOG_LEVEL` | `INFO` | |
| `DREAM_LOG_JSON` | `true` | structured JSON logs |
| `DREAM_TRIAGE_ENABLED` | `true` | skip small talk before any model call |
| `DREAM_TRACK_RECALL` | `true` | record which memories each search returns. Consolidation's decay rule depends on it |

### Database

| Variable | Default | |
|---|---|---|
| `DREAM_DATABASE_PROVIDER` | `mongodb` | `mongodb` or `postgres` |
| `DREAM_MONGODB_URI` | `mongodb://localhost:27017` | any MongoDB connection string, including Atlas |
| `DREAM_MONGODB_DATABASE` | `memdream` | |
| `DREAM_POSTGRES_DSN` | — | `postgresql://user:pass@host:5432/db`. Requires the `postgres` extra |
| `DREAM_POSTGRES_POOL_SIZE` | `10` | connections per process |

MemDream creates its collections or tables and indexes on startup.

> **PostgreSQL is in preview.** It implements the same interface as MongoDB, but
> has not yet been verified against a live server in the automated test suite.
> Use MongoDB for production today.

### LLM and embeddings

Chat and embeddings are chosen separately, so you can mix providers — for
example Claude for chat and OpenAI for embeddings, since Anthropic offers no
embeddings.

| Variable | Values |
|---|---|
| `DREAM_LLM_PROVIDER` | `azure_openai`, `openai`, `gemini`, `anthropic`, `groq`, `ollama`, `together`, `fireworks`, `openai_compatible`, `fake` |
| `DREAM_EMBEDDING_PROVIDER` | `azure_openai`, `openai`, `gemini`, `ollama`, `openai_compatible`, `fake` |

`fake` is a deterministic offline provider for tests and demos.

**Azure OpenAI**

```bash
DREAM_LLM_PROVIDER=azure_openai
DREAM_EMBEDDING_PROVIDER=azure_openai
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_API_VERSION=2024-10-21
AZURE_OPENAI_CHAT_DEPLOYMENT=gpt-4o-mini
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-small
AZURE_OPENAI_EMBEDDING_DIMENSIONS=1536
```

**OpenAI, Groq, Ollama, Together, Fireworks, or your own endpoint**

```bash
DREAM_LLM_PROVIDER=openai
DREAM_EMBEDDING_PROVIDER=openai
OPENAI_API_KEY=sk-...               # or DREAM_OPENAI_API_KEY
DREAM_OPENAI_CHAT_MODEL=            # blank: the provider's default
DREAM_OPENAI_EMBEDDING_MODEL=
DREAM_OPENAI_EMBEDDING_DIMENSIONS=0

# Groq for chat, OpenAI for embeddings
DREAM_LLM_PROVIDER=groq             GROQ_API_KEY=gsk_...
DREAM_EMBEDDING_PROVIDER=openai     OPENAI_API_KEY=sk-...

# Fully local, no key, no per-token cost
DREAM_LLM_PROVIDER=ollama
DREAM_EMBEDDING_PROVIDER=ollama

# Any OpenAI-compatible server: vLLM, LiteLLM, a proxy
DREAM_LLM_PROVIDER=openai_compatible
DREAM_OPENAI_BASE_URL=http://vllm.internal/v1
DREAM_OPENAI_CHAT_MODEL=mistral-7b
```

**Google Gemini** — `pip install ".[gemini]"`. Covers chat and embeddings.

```bash
DREAM_LLM_PROVIDER=gemini
DREAM_EMBEDDING_PROVIDER=gemini
GOOGLE_API_KEY=...                  # or GEMINI_API_KEY, or DREAM_GEMINI_API_KEY
DREAM_GEMINI_CHAT_MODEL=gemini-2.0-flash
DREAM_GEMINI_EMBEDDING_MODEL=gemini-embedding-001
DREAM_GEMINI_EMBEDDING_DIMENSIONS=768
```

**Anthropic Claude** — `pip install ".[anthropic]"`. Chat only.

```bash
DREAM_LLM_PROVIDER=anthropic
DREAM_EMBEDDING_PROVIDER=openai
ANTHROPIC_API_KEY=sk-ant-...        # or DREAM_ANTHROPIC_API_KEY
DREAM_ANTHROPIC_MODEL=claude-sonnet-4-5
OPENAI_API_KEY=sk-...
```

> **Changing the embedding model or its dimensions** makes existing vectors
> incomparable with new ones. Rebuild the index afterwards with
> `python -m scripts.rebuild_index`.

### Vector store

The database is the source of truth; the vector index holds a copy of each
memory's embedding and can always be rebuilt from it.

| `DREAM_VECTOR_PROVIDER` | Extra | Settings |
|---|---|---|
| `faiss` *(default)* | — | `DREAM_FAISS_PATH` (default `./data/faiss`), `DREAM_FAISS_MAX_LOADED_INDEXES` (128) |
| `qdrant` | `qdrant` | `DREAM_QDRANT_URL`, `DREAM_QDRANT_API_KEY`, `DREAM_QDRANT_COLLECTION` |
| `pgvector` | `pgvector` | `DREAM_POSTGRES_DSN`, `DREAM_PGVECTOR_TABLE` |
| `chroma` | `chroma` | `DREAM_CHROMA_PATH`, or `DREAM_CHROMA_HOST` and `DREAM_CHROMA_PORT`; `DREAM_CHROMA_COLLECTION` |
| `pinecone` | `pinecone` | `DREAM_PINECONE_API_KEY` and index settings — see `app/core/config.py` |
| `milvus` | `milvus` | see `app/core/config.py` |
| `azure_search` | `azure-search` | `DREAM_AZURE_SEARCH_API_KEY` and endpoint settings — see `app/core/config.py` |
| `memory` | — | in-process and not persisted. Tests and demos only |

FAISS is a library, not a server: its index is files on local disk, so it suits a
single API process. For several replicas, use a vector store that runs as a
service. See [Scaling](#scaling).

### Authentication

With no keys configured, the API accepts every request — acceptable only on a
private network behind your own gateway.

| Variable | |
|---|---|
| `DREAM_API_KEYS` | comma-separated shared keys. Callers send one as `X-API-Key`; `clientId` comes from the request |
| `DREAM_API_KEY_CLIENTS` | `key1:acme,key2:globex`. Each key is bound to a client, and the tenant comes from the key |
| `DREAM_API_KEY_REQUIRE_BINDING` | `true` refuses any key that is not bound to a client |

**For a multi-tenant service, bind every key** and set
`DREAM_API_KEY_REQUIRE_BINDING=true`. With shared keys, any valid key can name any
`clientId`.

To issue and revoke keys without restarting, insert documents into the `api_keys`
collection. Store only the key's SHA-256 digest:

```bash
python -c "import hashlib, secrets; k = 'sk_live_' + secrets.token_urlsafe(32); print(k); print(hashlib.sha256(k.encode()).hexdigest())"
```

```json
{ "_id": "key_acme_prod", "keyHash": "<the digest>", "clientId": "acme", "active": true }
```

Set `"active": false` to revoke. Changes take effect within 60 seconds.

### Background synthesis

| Variable | Default | |
|---|---|---|
| `DREAM_QUEUE_PROVIDER` | `inline` | `inline`: a task pool inside the API. `database`: the jobs collection is a queue claimed by a separate worker |
| `DREAM_DREAM__SYNTHESIS__MAX_CONCURRENT` | `2` | synthesis jobs a process runs at once |
| `DREAM_DREAM__SYNTHESIS__MAX_QUEUED` | `100` | waiting jobs before new ones are refused |
| `DREAM_DREAM__SYNTHESIS__MIN_MEMORIES` | `20` | active memories a user needs before synthesis looks for patterns |
| `DREAM_DREAM__SYNTHESIS__AUTO_TRIGGER` | `true` | run automatically as users accumulate memories |
| `DREAM_DREAM__SYNTHESIS__AUTO_TRIGGER_NEW_MEMORIES` | `20` | new memories that trigger a run |
| `DREAM_DREAM__SYNTHESIS__AUTO_TRIGGER_COOLDOWN_SECONDS` | `21600` | at most one automatic run per user per 6 hours |

With `DREAM_QUEUE_PROVIDER=database`, run the worker alongside the API:

```bash
python -m app.workers.synthesis_worker
```

### Consolidation

The nightly pass that retires memories. See
[consolidation](concepts.md#consolidation--forgetting-safely).

| Variable | Default | |
|---|---|---|
| `DREAM_DREAM__CONSOLIDATION__ENABLED` | `true` | retire memories whose validity window has closed |
| `DREAM_DREAM__CONSOLIDATION__SCHEDULE_ENABLED` | `true` | run the pass for every user once a day |
| `DREAM_DREAM__CONSOLIDATION__SCHEDULE_HOUR_UTC` | `0` | hour of day, UTC. Pick your quiet hour |
| `DREAM_DREAM__CONSOLIDATION__MAX_PER_RUN` | `500` | memories examined per user per pass |
| `DREAM_DREAM__CONSOLIDATION__DECAY_ENABLED` | `false` | also retire memories nothing has recalled |
| `DREAM_DREAM__CONSOLIDATION__DECAY_AFTER_DAYS` | `90` | |
| `DREAM_DREAM__CONSOLIDATION__DECAY_IMPORTANCE_FLOOR` | `0.5` | never decay at or above this importance |
| `DREAM_DREAM__CONSOLIDATION__RECALL_EPOCH` | empty | the date recall tracking began, e.g. `2026-09-14`. **Decay does nothing until this is set** |
| `DREAM_DREAM__COMPACTION__ENABLED` | `false` | fold crowds of similar low-value memories into summaries. One model call per group |
| `DREAM_DREAM__COMPACTION__IMPORTANCE_FLOOR` | `0.4` | never compact at or above this importance |
| `DREAM_DREAM__COMPACTION__MIN_MEMORIES` | `40` | eligible memories a user needs before compaction runs |
| `DREAM_DREAM__COMPACTION__MIN_AGE_DAYS` | `30` | never compact anything younger |
| `DREAM_DREAM__COMPACTION__MAX_CLUSTERS_PER_RUN` | `5` | caps model calls per user per pass |

**To enable decay safely:** set `RECALL_EPOCH` to today and leave
`DECAY_ENABLED=false`. Over the following weeks, run dry runs against real users —
[`POST /v1/dream/consolidate`](api.md#post-v1dreamconsolidate) with
`"decay": true`, or `python -m scripts.consolidate --all --dry-run --decay` — and
read the `consolidation_would_expire` log events. Turn it on once the candidates
stop surprising you. Do the same for compaction with `"compact": true`.

### Observability

| Variable | |
|---|---|
| `DREAM_OTEL_ENABLED` | `true` exports traces and metrics over OTLP. Requires the `otel` extra |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | your collector |
| `OTEL_SERVICE_NAME` | default `dream-memory` |

Every request logs one structured line with its path, status, duration and
request id. Send `X-Request-ID` from your services to correlate across systems.
[`GET /v1/metrics`](api.md#get-v1metrics) and
[`GET /v1/metrics/tokens`](api.md#get-v1metricstokens) show live counters for a
single process; [`GET /v1/dream/stats`](api.md#get-v1dreamstats) gives durable
per-user cost.

Cost figures use the prices under `pricing` in `config/dream.yaml`. Set them to
match your provider contract.

---

## Scaling

One process handles a lot: nearly all of a request's time is spent waiting on
the LLM, which costs an async server almost nothing.

When you need more than one API replica:

1. **Use a vector store that runs as a service** — Qdrant, pgvector, Pinecone,
   Milvus, Chroma in client/server mode, or Azure AI Search. FAISS files on one
   machine's disk cannot be shared safely across replicas.
2. **Set `DREAM_QUEUE_PROVIDER=database`** and run one or more synthesis workers,
   so all replicas share one synthesis queue. Workers claim jobs atomically; two
   cannot take the same one.
3. **Leave the nightly consolidation on in every replica.** It is idempotent:
   retiring an already-retired memory does nothing, so replicas repeat work rather
   than double it.

The database is the only stateful component to back up. The vector index can be
rebuilt from it.

---

## Production checklist

- [ ] `DREAM_IDENTITY_SALT` set to a long random value, stored as a secret, never changed
- [ ] API keys bound to clients, and `DREAM_API_KEY_REQUIRE_BINDING=true` on multi-tenant deployments
- [ ] The service reachable only through your gateway or private network, over TLS
- [ ] CORS: MemDream allows every origin by default. Restrict `allow_origins` in `app/main.py` if browsers can reach it directly
- [ ] `.env` and credentials kept out of version control
- [ ] Orchestrator probes: `/health` for liveness, `/ready` for readiness
- [ ] Database backups. The vector index does not need them
- [ ] `DREAM_DOMAIN` tested against real user messages — check the `NOOP` reasons in the audit log
- [ ] `pricing` in `config/dream.yaml` set to your provider's rates
- [ ] Decay and compaction measured with dry runs before enabling
- [ ] `DREAM_DREAM__CONSOLIDATION__SCHEDULE_HOUR_UTC` moved to your quiet hour

---

## Maintenance scripts

Run from the repository root with the service's environment.

| Command | |
|---|---|
| `python -m scripts.rebuild_index` | rebuild the vector index from the database — after changing embedding model, or losing index files |
| `python -m scripts.consolidate --user <id> --dry-run` | show what consolidation would retire for one user. `--all` for every user; `--decay`, `--decay-after <days>`, `--epoch <date>`, `--importance-floor <n>`, `--compact` to try settings. Drop `--dry-run` to apply |
| `python -m scripts.migrate_scope_key` | backfill the partition key on data written by versions before it existed |
| `python -m scripts.calibrate` | measure similarity bands for your embedding model, to tune thresholds |
| `python -m scripts.seed_demo --user <id> --wipe` | create a realistic, year-old demo account for trying consolidation |
| `python -m eval.run` | run the 250-case evaluation set against your configured providers |
