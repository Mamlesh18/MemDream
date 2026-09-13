# Python SDK

The MemDream SDK gives your Python agent long-term memory in a few lines. It
comes in two forms with **the same methods, arguments, return types and
exceptions**:

| | Embedded — `MemDream` | Hosted — `DreamClient` |
|---|---|---|
| **Runs** | the memory engine inside your process | calls a MemDream server over HTTP |
| **Deploy** | nothing | a [MemDream server](self-hosting.md) |
| **Needs** | a database your process can reach, and LLM credentials | the server's URL and an API key |
| **Best for** | getting started, scripts, notebooks, one service | several services or languages sharing one memory, central control |

Start embedded and move to hosted later — it changes one line:

```python
mem = MemDream(user_id="alice")                                              # embedded
mem = DreamClient("https://memory.example.com", api_key="sk_...", user_id="alice")  # hosted
```

Both have async equivalents, `AsyncMemDream` and `AsyncDreamClient`.

New to MemDream? Read [Concepts](concepts.md) first — scope, statuses and the
lifecycle explain most of what the methods below do.

---

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Scope](#scope)
- [Use it in an agent](#use-it-in-an-agent)
- **Reference**
  - [Constructors](#constructors)
  - [Remember](#remember) — [`add`](#add) · [`create`](#create) · [`update`](#update) · [`forget`](#forget) · [`purge`](#purge)
  - [Recall](#recall) — [`search`](#search) · [`get`](#get) · [`list`](#list) · [`history`](#history) · [`audit`](#audit)
  - [Dream](#dream) — [`synthesize`](#synthesize) · [`get_job`](#get_job) · [`wait_for`](#wait_for) · [`list_jobs`](#list_jobs) · [`stats`](#stats) · [`consolidate`](#consolidate)
  - [Server and lifecycle](#server-and-lifecycle)
  - [Return types](#return-types)
  - [Errors](#errors)
- [Differences between the two forms](#differences-between-the-two-forms)

---

## Install

MemDream is installed from GitHub:

```bash
pip install "memdream @ git+https://github.com/Mamlesh18/MemDream.git"
```

With optional providers:

```bash
pip install "memdream[gemini] @ git+https://github.com/Mamlesh18/MemDream.git"       # Google Gemini
pip install "memdream[anthropic] @ git+https://github.com/Mamlesh18/MemDream.git"    # Claude
pip install "memdream[postgres] @ git+https://github.com/Mamlesh18/MemDream.git"     # PostgreSQL
```

Python 3.10 or newer. OpenAI, Azure OpenAI, Groq, Ollama, Together and Fireworks
need no extra.

---

## Quick start

### Embedded

```python
from memdream import MemDream

mem = MemDream(user_id="user_123")

mem.add("Hi, I'm Priya. I live in Chennai.")
mem.add("I'm allergic to peanuts and I always order the veg thali for lunch.")

result = mem.add("I live in Bangalore now.")
print(result.action)                   # SUPERSEDE
print(result.superseded_memory_ids)    # ['mem_002e9cd0a9d94445bb4edad2']

memories = mem.search("where does the user live?", latest_only=True)
print(memories.as_context())
# - User lives in Bangalore.
```

The embedded form reads its configuration — database, LLM and embedding
provider — from environment variables or a `.env` file. See
[Configuration](#configuration).

### Hosted

```python
from memdream import DreamClient

mem = DreamClient(
    "https://memory.example.com",
    api_key="sk_live_...",
    user_id="user_123",
)

mem.add("I live in Bangalore now.")
print(mem.search("where does the user live?", latest_only=True).as_context())
```

The client needs no configuration beyond its constructor: the server holds the
database and model credentials.

### Async

```python
from memdream import AsyncMemDream        # or AsyncDreamClient

async with AsyncMemDream(user_id="user_123") as mem:
    await mem.add("I live in Bangalore now.")
    memories = await mem.search("where does the user live?")
```

### Two call styles

Every method is available flat and under a namespace. They are the same call:

```python
mem.add("...")              mem.memory.add("...")
mem.search("...")           mem.memory.search("...")
mem.synthesize()            mem.dream.synthesize()
```

`memory.*` holds [Remember](#remember) and [Recall](#recall); `dream.*` holds
[Dream](#dream).

---

## Configuration

*Embedded only.* A hosted client is configured by its constructor alone.

The embedded SDK builds the same engine the server runs, so it is configured the
same way: `DREAM_*` environment variables, or a `.env` file in the working
directory. The minimum is a database and an LLM and embedding provider:

```bash
# .env
DREAM_MONGODB_URI=mongodb://localhost:27017
DREAM_MONGODB_DATABASE=memdream
DREAM_IDENTITY_SALT=a-long-random-string-you-never-change

DREAM_LLM_PROVIDER=openai
DREAM_EMBEDDING_PROVIDER=openai
OPENAI_API_KEY=sk-...
```

Any setting can also be passed to the constructor, which is convenient for one
or two values:

```python
mem = MemDream(
    user_id="user_123",
    llm_provider="gemini",
    embedding_provider="gemini",
    gemini_api_key="...",
    domain="A restaurant ordering assistant. Remember contact and delivery details, "
           "dietary needs, dislikes, and usual orders per meal.",
)
```

Every setting, provider and default is listed in [Self-hosting](self-hosting.md).

### Try it with no infrastructure

The offline providers need no database, key or network. Add
`pip install mongomock-motor`, then:

```python
from mongomock_motor import AsyncMongoMockClient
from app.repositories.mongo import MongoDatabase
from memdream import MemDream

offline = MemDream(
    user_id="alice",
    database=MongoDatabase(AsyncMongoMockClient(tz_aware=True), "demo"),
    llm_provider="fake",
    embedding_provider="fake",
    vector_provider="memory",
    identity_salt="demo",
)
```

The fake providers are deterministic heuristics, not stubs — good for seeing the
decision flow and for tests, not for judging extraction quality.

---

## Scope

Every call runs inside a [scope](concepts.md#scope-whose-memory-is-it). Set it
once on the constructor; every call inherits it:

```python
mem = MemDream(user_id="user_123", agent_id="support-bot", client_id="acme")
mem.add("I prefer email receipts.")        # scoped to user_123 / support-bot / acme
```

Override any part per call:

```python
mem.search("receipts", user_id="user_456")
mem.add("Deliver to the office.", namespace={"env": "staging"})
```

| Keyword | |
|---|---|
| `user_id` | the user. This or `phone_number` is required |
| `phone_number` | derive the user id from a phone number instead |
| `agent_id` | which agent or bot |
| `client_id` | the tenant |
| `namespace` | further dimensions, e.g. `{"env": "prod"}`. Merged with the constructor's |
| `bot_id` | previous name for `agent_id`, still accepted |

**A misspelt keyword raises `TypeError`.** `mem.search("x", clinet_id="acme")`
fails immediately instead of quietly searching a different partition and
returning an empty result that looks real.

A call with no user raises `ValueError` before anything is sent.

---

## Use it in an agent

Recall before the model answers; remember after.

```python
from memdream import MemDream

mem = MemDream()          # one instance per process — it holds connection pools

def handle(user_id: str, message: str) -> str:
    memories = mem.search(message, latest_only=True, limit=8, user_id=user_id)

    reply = llm(
        system=f"You are a helpful assistant.\n\nWhat you know about this user:\n"
               f"{memories.as_context()}",
        user=message,
    )

    mem.add(message, user_id=user_id)      # after: the message is not context for itself
    return reply
```

Three things worth copying:

- **Recall before, remember after.** Otherwise the current message becomes
  context for answering itself.
- **Create the instance once.** It owns database and HTTP connection pools;
  creating one per request pays setup every time and exhausts connection limits
  under load.
- **Move `add` off the reply path** when you can — a task queue, a background
  thread, or `asyncio.create_task` with `AsyncMemDream`. Remembering takes one to
  two model calls; the user should not wait for them.

---

# Reference

Signatures below are identical for `MemDream`, `AsyncMemDream`, `DreamClient` and
`AsyncDreamClient`. On the async classes every method is a coroutine. `**scope`
means the [scope keywords](#scope).

## Constructors

### `MemDream` / `AsyncMemDream`

```python
MemDream(
    *,
    user_id: str | None = None,
    phone_number: str | None = None,
    agent_id: str | None = None,
    client_id: str | None = None,
    namespace: dict[str, str] | None = None,
    bot_id: str | None = None,
    settings: Settings | None = None,
    container: DreamContainer | None = None,
    database: Database | None = None,
    **overrides,
)
```

| Argument | |
|---|---|
| `user_id` … `bot_id` | default [scope](#scope) for every call |
| `settings` | a complete `app.core.config.Settings`. Default: read from the environment |
| `**overrides` | individual settings, e.g. `llm_provider="gemini"` |
| `database` | an already-connected database to use instead of opening one |
| `container` | an already-built engine to adopt, e.g. the one your MemDream server app built. The SDK will not close what it did not open |

Construction is cheap: connections open on the first call, or when you call
`start()`.

### `DreamClient` / `AsyncDreamClient`

```python
DreamClient(
    base_url: str = "http://localhost:8000",
    *,
    api_key: str | None = None,
    user_id: str | None = None,
    phone_number: str | None = None,
    agent_id: str | None = None,
    client_id: str | None = None,
    namespace: dict[str, str] | None = None,
    bot_id: str | None = None,
    timeout: float = 60.0,
    http_client: httpx.Client | None = None,     # httpx.AsyncClient for AsyncDreamClient
)
```

| Argument | |
|---|---|
| `base_url` | the MemDream server |
| `api_key` | sent as `X-API-Key` on every request, including through your own `http_client` |
| `user_id` … `bot_id` | default [scope](#scope) for every call |
| `timeout` | seconds per request |
| `http_client` | your own configured `httpx` client — proxies, retries, custom transport. You close it |

---

## Remember

### `add`

Remember a message. MemDream extracts the durable facts and decides what to do
with each.

```python
add(
    prompt: str,
    *,
    message_id: str | None = None,
    timestamp: datetime | None = None,
    metadata: dict | None = None,
    raw: bool = False,
    **scope,
) -> AddResult
```

| Argument | |
|---|---|
| `prompt` | the user's message, 1–20,000 characters |
| `message_id` | your id for the message, stored on the memory as provenance |
| `timestamp` | when the message was sent, if not now. Anchors phrases like *"last month"* |
| `metadata` | stored on the resulting memories and returned untouched |
| `raw` | store `prompt` verbatim as one memory instead of extracting facts from it. The add / supersede / merge decision still runs |

```python
result = mem.add(
    "I'm allergic to peanuts and I always order the veg thali for lunch.",
    message_id="msg_0042",
    metadata={"channel": "whatsapp"},
)

result.action            # 'ADD'
result.extracted_count   # 2
for decision in result.results:
    print(decision.action, decision.content)
# ADD User is allergic to peanuts.
# ADD User always orders the veg thali for lunch.

mem.add("hey thanks!").remembered     # False — small talk, no model call
```

Returns an [`AddResult`](#addresult). HTTP: [`POST /v1/memories`](api.md#post-v1memories).

### `create`

Store a memory exactly as written — no extraction, no decision. For importing
facts you already know, or storing something extraction would not keep.

```python
create(
    content: str,
    *,
    memory_type: str = "other",
    importance: float = 0.6,
    confidence: float = 1.0,
    explicit: bool = True,
    temporal_nature: str = "current",
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    metadata: dict | None = None,
    allow_duplicate: bool = False,
    **scope,
) -> MemoryDetail
```

| Argument | |
|---|---|
| `content` | 1–4,000 characters. Write it in the third person, as extraction does: `"User is allergic to peanuts."` |
| `memory_type` | any string; see [memory types](concepts.md#the-memory-object) |
| `importance` `confidence` | 0–1 |
| `explicit` | `False` marks it as inferred rather than stated |
| `temporal_nature` | `permanent`, `current`, `temporary`, `historical` or `future` |
| `valid_from` `valid_until` | when the fact is true. Consolidation retires it after `valid_until` |
| `allow_duplicate` | by default an identical active memory is returned instead of a second copy |

```python
detail = mem.create(
    "User is allergic to peanuts.",
    memory_type="constraint",
    importance=0.95,
    temporal_nature="permanent",
)
detail.memory.memory_id     # 'mem_…'
```

Returns a [`MemoryDetail`](#memorydetail). HTTP: [`POST /v1/memories/manual`](api.md#post-v1memoriesmanual).

### `update`

Correct an active memory in place. Only the fields you pass change; the id stays
the same.

```python
update(memory_id: str, **changes) -> MemoryDetail
```

Updatable: `content`, `memory_type`, `importance`, `confidence`, `explicit`,
`temporal_nature`, `valid_from`, `valid_until`, `metadata`. Scope keywords may be
mixed in.

```python
mem.update("mem_9eff9c243db54ffda6c75295", importance=0.9, metadata={"verified": True})
```

An edit is a correction, not a change in the user's life, so it does not create
history. When the user's situation changes, use `add` and let MemDream supersede.

Raises `ValueError` with no fields, `TypeError` for an unknown field, and
[`DreamAPIError`](#errors) with status **409** for a `superseded` or `merged`
memory — history is never rewritten. HTTP: [`PATCH /v1/memories/{memoryId}`](api.md#patch-v1memoriesmemoryid).

### `forget`

Retire a memory, or erase it.

```python
forget(memory_id: str, *, permanent: bool = False, **scope) -> dict
```

By default the memory moves to `expired`: it leaves search results but stays
auditable. This is what *"forget that"* should do. `permanent=True` deletes the
row and its relationships and cannot be undone.

```python
mem.forget("mem_9eff9c243db54ffda6c75295")
# {'memoryId': 'mem_9eff9c243db54ffda6c75295', 'result': 'expired', 'permanent': False, ...}

mem.forget("mem_9eff9c243db54ffda6c75295", permanent=True)
# {..., 'result': 'deleted', 'permanent': True}
```

HTTP: [`DELETE /v1/memories/{memoryId}`](api.md#delete-v1memoriesmemoryid).

### `purge`

Erase everything for one user: memories, relationships, audit trail and jobs.
Irreversible.

```python
purge(*, confirm: bool = False, **scope) -> dict
```

`confirm=True` is required; without it, `ValueError`.

```python
mem.purge(confirm=True, user_id="user_123")
# {'deletedMemories': 5, 'deletedRelations': 0, 'deletedOperations': 9,
#  'deletedJobs': 1, 'operationId': None}
```

HTTP: [`DELETE /v1/memories`](api.md#delete-v1memories).

---

## Recall

### `search`

Find the memories that answer a question, [hybrid-ranked](concepts.md#how-recall-ranks-memories).

```python
search(
    query: str,
    *,
    limit: int = 10,
    latest_only: bool = False,
    include_superseded: bool = True,
    include_merged: bool = False,
    include_expired: bool = False,
    memory_types: list[str] | None = None,
    min_score: float | None = None,
    as_of: datetime | None = None,
    **scope,
) -> SearchResult
```

| Argument | |
|---|---|
| `query` | 1–8,000 characters |
| `limit` | 1–100 |
| `latest_only` | only `active` memories — "what is true now?". Overrides the `include_*` flags |
| `include_superseded` | include replaced facts, ranked below current ones. Default on |
| `include_merged` `include_expired` | widen further |
| `memory_types` | e.g. `["preference", "constraint"]` |
| `min_score` | 0–1; drop weaker matches |
| `as_of` | only memories true at that instant — "where did they live in March?" |

```python
results = mem.search("what should I avoid cooking?", latest_only=True, min_score=0.4)

for memory in results:
    print(f"{memory.score:.2f}  {memory.content}")
# 0.62  User is allergic to peanuts.

prompt_block = results.as_context()
# '- User is allergic to peanuts.'
```

Returns a [`SearchResult`](#searchresult). HTTP: [`POST /v1/memories/search`](api.md#post-v1memoriessearch).

### `get`

One memory with its relationships.

```python
get(memory_id: str, **scope) -> MemoryDetail
```

```python
detail = mem.get("mem_7c41d2a09b8e4f3a9d6b1e20")
detail.memory.content                    # 'User lives in Bangalore.'
detail.relationships[0].relation_type    # 'supersedes'
detail.relationships[0].content          # 'User lives in Chennai.'
```

Raises [`DreamNotFoundError`](#errors) if it does not exist in this scope.
HTTP: [`GET /v1/memories/{memoryId}`](api.md#get-v1memoriesmemoryid).

### `list`

Browse memories without a query, newest first.

```python
list(
    *,
    limit: int = 50,
    offset: int = 0,
    status: list[str] | None = None,
    memory_type: list[str] | None = None,
    **scope,
) -> list[MemoryRecord]
```

`status` defaults to `["active"]`. `limit` is 1–200.

```python
everything = mem.list(status=["active", "superseded", "merged", "expired"], limit=200)
preferences = mem.list(memory_type=["preference"])
```

HTTP: [`GET /v1/memories`](api.md#get-v1memories).

### `history`

Follow a memory's supersede chain back to the original fact, newest first.

```python
history(memory_id: str, **scope) -> list[MemoryRecord]
```

```python
for memory in mem.history("mem_7c41d2a09b8e4f3a9d6b1e20"):
    print(memory.status, memory.content)
# active      User lives in Bangalore.
# superseded  User lives in Chennai.
```

HTTP: [`GET /v1/memories/{memoryId}/history`](api.md#get-v1memoriesmemoryidhistory).

### `audit`

Every decision MemDream made for a user, newest first — `NOOP`s included, which
is how you find out why something was not remembered.

```python
audit(
    *,
    memory_id: str | None = None,
    action: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
    **scope,
) -> list[dict]
```

With `memory_id`, only the decisions that touched that memory. `action` filters
(e.g. `["NOOP"]`) and `offset` apply to the user-wide log only.

```python
for op in mem.audit(action=["NOOP"], limit=5):
    print(op["inputContent"], "→", op["reason"])
# hey thanks! → greeting or acknowledgement
```

Each entry is an [audit operation](api.md#audit-operation). HTTP:
[`GET /v1/memories/audit/operations`](api.md#get-v1memoriesauditoperations) and
[`GET /v1/memories/{memoryId}/audit`](api.md#get-v1memoriesmemoryidaudit).

---

## Dream

### `synthesize`

Look for [patterns](concepts.md#synthesis--finding-patterns) across a user's
memories.

```python
synthesize(
    *,
    wait: bool = False,
    force: bool = False,
    min_memories: int | None = None,
    **scope,
) -> DreamJob
```

| Argument | |
|---|---|
| `wait` | run it before returning, instead of in the background |
| `force` | re-evaluate groups that already produced a pattern |
| `min_memories` | active memories needed before looking, for this run only (≥ 2). Default 20 |

```python
job = mem.synthesize(wait=True)
job.status              # 'completed'
job.patterns_created    # 1
job.created_pattern_ids # ['pat_4f1c9e2a7b3d4e5f8a6c0b12']
```

If a run is already queued or running for the user, that job is returned instead
of starting another. Synthesis also runs automatically; you rarely need to call
this. HTTP: [`POST /v1/dream/synthesize`](api.md#post-v1dreamsynthesize).

### `get_job`

```python
get_job(job_id: str, **scope) -> DreamJob
```

HTTP: [`GET /v1/dream/jobs/{jobId}`](api.md#get-v1dreamjobsjobid).

### `wait_for`

Poll a job until it finishes.

```python
wait_for(job_id: str, *, timeout: float = 300.0, poll_interval: float = 2.0, **scope) -> DreamJob
```

```python
job = mem.synthesize()
job = mem.wait_for(job.job_id, timeout=120)
```

Raises `TimeoutError` if the job is still running after `timeout` seconds.

### `list_jobs`

```python
list_jobs(*, limit: int = 20, offset: int = 0, **scope) -> list[DreamJob]
```

HTTP: [`GET /v1/dream/jobs`](api.md#get-v1dreamjobs).

### `stats`

Memory counts, decision tallies, token spend and estimated cost for a user.

```python
stats(**scope) -> dict
```

```python
stats = mem.stats()
stats["counts"]                        # {'active': 4, 'superseded': 0, 'merged': 0, 'expired': 1}
stats["actions"]                       # {'ADD': 4, 'NOOP': 2, 'MANUAL_ADD': 1, ...}
stats["tokens"]["totals"]["estimatedCost"]   # 0.000213
```

All `NOOP` usually means extraction is too strict for your messages; all `ADD`
means supersede and merge are not firing. HTTP: [`GET /v1/dream/stats`](api.md#get-v1dreamstats).

### `consolidate`

Run the nightly [consolidation pass](concepts.md#consolidation--forgetting-safely)
now, for one user. **A dry run unless you say otherwise.**

```python
consolidate(
    *,
    dry_run: bool = True,
    compact: bool = False,
    compact_min_memories: int | None = None,
    decay: bool | None = None,
    decay_after_days: int | None = None,
    decay_importance_floor: float | None = None,
    recall_epoch: str | date | None = None,
    **scope,
) -> ConsolidationReport
```

| Argument | |
|---|---|
| `dry_run` | report what would happen and change nothing. **Default `True`** |
| `compact` | also fold crowds of near-identical, low-value memories into summaries. Costs one model call per group |
| `compact_min_memories` | eligible memories a user needs before compaction runs (≥ 2). Default 40 |
| `decay` | apply the recall-decay rule in this run |
| `decay_after_days` | not recalled for this many days makes a memory a candidate. Default 90 |
| `decay_importance_floor` | memories at or above this importance are never decayed. Default 0.5 |
| `recall_epoch` | the date recall tracking began. **Decay cannot fire without it** |

Every override applies to this call only; none changes what the nightly pass does.

```python
# 1. See what a 30-day window would retire.
report = mem.consolidate(decay=True, decay_after_days=30, recall_epoch="2026-01-01")
report.dry_run        # True
report.lapsed         # 2    validity window closed
report.decayed        # 54   not recalled in 30 days
report.protected      # 4    kept: an active pattern rests on them
report.would_retire   # 56

# 2. Apply it once the list looks right.
report = mem.consolidate(dry_run=False, decay=True, decay_after_days=30,
                         recall_epoch="2026-01-01")
report.expired        # 56
report.expired_ids    # ['mem_…', …]   first 20
```

Retired memories move to `expired`; folded ones to `merged` with a pointer to
their summary. Nothing is deleted. HTTP: [`POST /v1/dream/consolidate`](api.md#post-v1dreamconsolidate).

---

## Server and lifecycle

| Method | Embedded | Hosted |
|---|---|---|
| `health()` | per-dependency checks: `database`, `vector_store`, `queue`, `embeddings`, `llm` | liveness of the server: `{"status": "ok", ...}` |
| `ready()` | — | readiness with per-dependency checks ([`GET /ready`](api.md#get-ready)) |
| `metrics()` | — | the server's counters and latency histograms |
| `token_usage()` | — | the server's token spend by operation |
| `start()` | open connections now rather than on the first call. Returns the instance | — |
| `close()` | close connections the SDK opened | close the HTTP client, if the SDK created it |
| context manager | `with MemDream(...) as mem:` / `async with AsyncMemDream(...)` | `with DreamClient(...) as mem:` / `async with AsyncDreamClient(...)` |
| `container` | the underlying engine, for anything the SDK does not cover | — |

In a web server, call `start()` at startup so the first user's request does not
pay for connection setup.

---

## Return types

All return types are plain dataclasses importable from `memdream`. Most keep the
full payload in `raw`, so a field added by a newer server is still reachable.

### `AddResult`

| Attribute | |
|---|---|
| `action` | `ADD`, `SUPERSEDE`, `MERGE` or `NOOP` — the most significant decision |
| `memory_id` | the memory that decision produced |
| `superseded_memory_ids` `merged_memory_ids` | what that decision replaced or absorbed |
| `results` | `list[DecisionResult]` — one per extracted fact |
| `extracted_count` | facts found in the message |
| `request_id` | the server's request id (hosted only) |
| `remembered` | property: `True` if anything was stored |

### `DecisionResult`

`action`, `memory_id`, `content`, `reason`, `superseded_memory_ids`,
`merged_memory_ids`, `operation_id`.

### `SearchResult`

Iterable, indexable and sized: `for m in results`, `results[0]`, `len(results)`.

| Attribute | |
|---|---|
| `memories` | `list[MemoryRecord]`, best first, each with `score` and `score_breakdown` |
| `count` | number of results |
| `as_context(separator="\n")` | the memories as `- …` lines, ready for a prompt |

### `MemoryRecord`

| Attribute | |
|---|---|
| `memory_id` `content` `memory_type` `status` | |
| `importance` `confidence` | 0–1 |
| `created_at` `updated_at` `valid_from` `valid_until` | `datetime` or `None` |
| `superseded_by` `merged_into` | the replacing memory's id |
| `source_memory_ids` | evidence, for patterns and summaries |
| `score` `score_breakdown` | on search results only |
| `metadata` `raw` | |
| `is_active` | property |

Every other [memory field](concepts.md#the-memory-object) is in `raw`.

### `MemoryDetail`

| Attribute | |
|---|---|
| `memory` | `MemoryRecord` |
| `relationships` | `list[RelatedMemory]` — what it supersedes, was merged from, and so on |
| `source_memories` | what a pattern or summary was built from |
| `derived_memories` | patterns built on this memory |
| `superseded_by` `merged_into` | |

`RelatedMemory` has `memory_id`, `relation_type`, `content`, `status`, `memory_type`.
Relation types: `supersedes`, `superseded_by`, `merged_into`, `merged_from`,
`derived_from`, `supports`.

### `DreamJob`

`job_id`, `status` (`queued`, `running`, `completed`, `failed`, `skipped`),
`patterns_created`, `patterns_updated`, `patterns_skipped`, `memories_analyzed`,
`clusters_evaluated`, `created_pattern_ids`, `error`, `duration_ms`, `raw`, and
the property `is_finished`.

### `ConsolidationReport`

| Attribute | |
|---|---|
| `dry_run` | whether anything was changed |
| `scanned` | active memories examined |
| `lapsed` | validity window closed |
| `decayed` | decay candidates — reported even when decay was not applied |
| `expired` | actually retired. Always 0 in a dry run |
| `protected` | kept because an active pattern rests on them |
| `expired_ids` | first 20 retired ids |
| `clusters` `compacted` `absorbed` `rejected` | compaction: groups found, summaries written, memories folded, summaries refused |
| `decay_applied` | whether decay was in force for this run |
| `duration_ms` | |
| `would_retire` | property: what applying this run would retire |

---

## Errors

Both forms raise the same exceptions for the same failures.

```python
from memdream import DreamAPIError, DreamNotFoundError, DreamValidationError

try:
    mem.update(memory_id, content="User lives in Pune.")
except DreamNotFoundError:
    ...                                   # not in this scope
except DreamAPIError as error:
    if error.status_code == 409:
        ...                               # it is history; use add() instead
    raise
```

| Exception | Raised when | `status_code` |
|---|---|---|
| `DreamValidationError` | an argument failed validation — empty prompt, importance above 1 | 422 |
| `DreamNotFoundError` | the memory or job does not exist in this scope | 404 |
| `DreamAuthError` | the API key is missing, invalid, or bound to a different client *(hosted)* | 401 |
| `DreamAPIError` | anything else: editing history (409), a provider failure (502), a server error (500) | as returned |
| `DreamConnectionError` | the server could not be reached or timed out *(hosted)* | — |
| `ValueError` | no user in scope; `purge()` without `confirm=True`; `update()` with nothing to change | — |
| `TypeError` | an unknown keyword — usually a misspelt scope argument | — |

`DreamValidationError`, `DreamNotFoundError` and `DreamAuthError` are subclasses
of `DreamAPIError`, which carries `message`, `status_code`, `code` (the API's
[error code](api.md#errors)), `details` and `request_id` — include the request id
when reporting a problem. Every SDK exception except `ValueError` and `TypeError`
derives from `memdream.exceptions.DreamError`.

---

## Differences between the two forms

The surface is the same. What differs is where the work happens:

| | Embedded | Hosted |
|---|---|---|
| Model and database credentials | in your process | on the server |
| `health()` | dependency checks | server liveness |
| `ready()` `metrics()` `token_usage()` | not available | available |
| `AddResult.request_id` | `None` | the server's request id |
| `synthesize()` without `wait` | runs in a background task in your process — keep the process alive until it finishes | runs on the server |
| Nightly consolidation | only in a running MemDream server | runs on the server |
| Connection pools | one per `MemDream` instance | shared by every client of the server |
