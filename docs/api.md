# API reference

Base URL: `http://localhost:8000`. Interactive docs at `/docs`, OpenAPI schema
at `/openapi.json`.

---

## Conventions

**Everything is camelCase**, in both directions.

**Every memory endpoint is scoped.** This includes reads by id: a memory id is a
random handle, not an authorisation token, so possessing one is not enough to
read the record. A memory belonging to another tenant reports `404`, not `403` —
indistinguishable from not existing.

**Only the user is required.** Identity arrives as `userId`, or as `phoneNumber`
for MemDream to derive one from. Everything else is an optional partition
dimension:

| Field | | |
|---|---|---|
| `userId` \| `phoneNumber` | **required** | the isolation boundary |
| `agentId` | optional | which agent or bot, when a deployment has several |
| `clientId` | optional | the tenant, for platforms serving several customers |
| `namespace` | optional | further dimensions: environment, region, workspace |
| `botId` | optional | previous name for `agentId`, still accepted |

A single-agent app sends `userId` and nothing else. Adding a dimension later
creates a *new* partition rather than widening the old one, so introduce them
before you have data, not after.

`POST` bodies carry scope as fields; `GET` endpoints take them as query
parameters.

**Authentication.** `DREAM_API_KEYS` (comma-separated) turns it on; send the key
as `X-API-Key`. Empty means no authentication — appropriate behind a gateway on
a private network, which is why it is opt-in rather than mandatory.

For multi-tenant deployments use `DREAM_API_KEY_CLIENTS=key1:acme,key2:globex`
instead. A **bound** key carries its tenant, so `clientId` comes from the
credential rather than the request body, and a body naming a different client is
refused with `401` rather than silently overridden. Without this, any valid key
could name any `clientId` and read another customer's memories.

**Request ids.** Send `X-Request-ID` to correlate with your own logs, or read
the one the service generates back from the response header.

### Errors

```json
{
  "error": {
    "code": "not_found",
    "message": "Memory mem_abc not found",
    "details": {}
  }
}
```

| Status | `code` | Meaning |
|---|---|---|
| 401 | `unauthorized` | missing or invalid `X-API-Key` |
| 403 | `tenant_isolation_violation` | a scope check failed internally; should never surface |
| 404 | `not_found`, `job_not_found` | absent, or outside the requesting scope |
| 409 | `invalid_state_transition` | e.g. merging a memory into itself |
| 422 | `validation_error` | request body failed validation |
| 500 | `internal_error` | unexpected; the response carries `requestId` |
| 502 | `provider_error`, `llm_response_error` | an upstream provider failed |

---

## POST /v1/memories

Extract durable memories from a message and let Dream decide what to do with
each one.

```json
{
  "prompt": "I moved from Chennai to Bangalore last month.",
  "phoneNumber": "+919999999999",
  "botId": "bot_123",
  "clientId": "client_456"
}
```

| Field | Type | Notes |
|---|---|---|
| `prompt` | string | required, ≤ 20 000 chars |
| `clientId` `botId` | string | required |
| `phoneNumber` | string | required unless `userId` is given |
| `userId` | string | supply instead of `phoneNumber` if you have your own identifier |
| `messageId` | string | recorded as provenance |
| `timestamp` | ISO-8601 | when the message was sent; anchors "last month" during extraction |
| `metadata` | object | stored on any memory this creates |
| `raw` | bool | store the prompt verbatim as one memory, skipping extraction but keeping deduplication and consolidation |

**Response**

```json
{
  "action": "SUPERSEDE",
  "memoryId": "mem_a1b2c3",
  "supersededMemoryIds": ["mem_x9y8z7"],
  "mergedMemoryIds": [],
  "results": [
    {
      "action": "SUPERSEDE",
      "memoryId": "mem_a1b2c3",
      "content": "User lives in Bangalore.",
      "reason": "new city Bangalore contradicts stored city Chennai",
      "supersededMemoryIds": ["mem_x9y8z7"],
      "mergedMemoryIds": [],
      "operationId": "op_44d1"
    }
  ],
  "extractedCount": 1,
  "requestId": "req_7f3a"
}
```

One message can hold several facts, so **`results` is the complete answer**. The
top-level `action` / `memoryId` / `supersededMemoryIds` mirror the most
significant result — a consolidation outranks an addition — which keeps the
single-fact case, by far the most common, as simple as it looks.

`action: "NOOP"` with `extractedCount: 0` means nothing durable was found. That
is a normal outcome for greetings and questions, not an error. `GET
/v1/memories/audit/operations` records why.

---

## POST /v1/memories/search

```json
{
  "query": "Where does the user currently live?",
  "phoneNumber": "+919999999999",
  "botId": "bot_123",
  "clientId": "client_456",
  "limit": 10,
  "latestOnly": true
}
```

| Field | Default | Notes |
|---|---|---|
| `query` | required | |
| `limit` | 10 | 1–100 |
| `latestOnly` | `false` | active only — "what is true now?". Overrides the `include*` flags |
| `includeSuperseded` | `true` | history, ranked below current facts |
| `includeMerged` | `false` | usually redundant: merged content lives in the canonical memory |
| `includeExpired` | `false` | |
| `memoryTypes` | all | open vocabulary |
| `minScore` | none | 0–1, on the final hybrid score |
| `asOf` | none | point-in-time: only memories whose validity window contains this instant |

**Response**

```json
{
  "memories": [
    {
      "memoryId": "mem_a1b2c3",
      "content": "User lives in Bangalore.",
      "memoryType": "profile",
      "status": "active",
      "importance": 0.75,
      "confidence": 0.95,
      "createdAt": "2026-08-13T10:00:00Z",
      "validFrom": "2026-07-01T00:00:00Z",
      "validUntil": null,
      "supersededBy": null,
      "sourceMemoryIds": ["mem_x9y8z7"],
      "score": 0.8123,
      "scoreBreakdown": {
        "semanticSimilarity": 0.71,
        "importance": 0.75,
        "confidence": 0.95,
        "recency": 0.99,
        "status": 1.0
      }
    }
  ],
  "count": 1,
  "requestId": "req_7f3a"
}
```

Ranking blends similarity with importance, confidence, recency and status — not
top-k cosine. `scoreBreakdown` is there so a ranking can be explained rather
than guessed at; weights are configurable under `retrieval.weights`.

Embeddings are never returned.

---

## The manual endpoints

`POST /v1/memories` is the conversational path: it decides *what* is worth
remembering. The four endpoints below are the deliberate path — importing known
facts, correcting a bad extraction, honouring "forget that", answering an
erasure request. They skip extraction and Dream reasoning entirely, because you
have already said exactly what you want.

All four still write an audit row. A human editing memory by hand is more worth
recording than a routine automated decision, not less.

---

## POST /v1/memories/manual

Write a memory verbatim. Returns `201`.

```json
{
  "content": "User is allergic to peanuts.",
  "memoryType": "constraint",
  "importance": 0.9,
  "userId": "user_123"
}
```

| Field | Type | Notes |
|---|---|---|
| `content` | string | required, ≤ 4000 chars |
| `memoryType` | string | default `other`. Open vocabulary |
| `importance` | 0–1 | default `0.6` |
| `confidence` | 0–1 | default `1.0` — you asserted it, so nothing was inferred |
| `explicit` | bool | default `true`. `false` marks it as inferred rather than stated |
| `temporalNature` | string | default `current`. `permanent` protects it from supersede |
| `validFrom` `validUntil` | ISO-8601 | when the fact is true. `validFrom` defaults to now |
| `metadata` | object | stored on the memory |
| `allowDuplicate` | bool | default `false` |

Write content **in the third person** (`"User is allergic to peanuts."`),
matching what the extractor produces, so retrieval treats it the same way.

An identical active memory is returned as-is rather than duplicated, so a
re-run importer converges instead of multiplying. `allowDuplicate: true` forces
a second row.

Everything written here carries `metadata.source: "manual"`, so it stays
distinguishable from anything the engine decided.

**Response** — the same shape as `GET /v1/memories/{memoryId}`.

---

## PATCH /v1/memories/{memoryId}

Correct an active memory in place. Only the fields you send change.

```json
{
  "content": "User is allergic to peanuts and shellfish.",
  "importance": 0.95,
  "userId": "user_123"
}
```

Accepts any subset of `content`, `memoryType`, `importance`, `confidence`,
`explicit`, `temporalNature`, `validFrom`, `validUntil`, `metadata`. Sending
none of them is `422` — an update that changes nothing is a mistake worth
reporting rather than a no-op worth accepting.

The memory **keeps its id**, so anything referencing it stays valid. Changing
`content` re-embeds it in the same call, because an index that disagrees with
the text it points at is worse than a stale one.

`metadata` is **merged**, not replaced: send only the keys you want to change.
Every edit stamps `metadata.lastManualEdit`, so a hand-corrected memory is
identifiable long after the audit row has scrolled away.

An unrecognised `temporalNature` is ignored rather than rejected, so a typo
leaves the rest of the update applied.

Editing is deliberately *not* a supersede. An operator fixing a bad extraction
is correcting a record; a supersede chain would claim the user's situation
changed, which would put a falsehood into the history. If the world changed
rather than the record being wrong, `POST /v1/memories` and let Dream supersede
it properly.

Only `active` memories can be edited — superseded and merged rows are history,
and history is not rewritten (`409`).

**Response** — the same shape as `GET /v1/memories/{memoryId}`.

---

## DELETE /v1/memories/{memoryId}

Query: scope, plus `permanent` (default `false`).

Two verbs behind one URL, because "stop using this" and "destroy this" carry
very different risk, and merging them is how data gets lost by accident.

| | |
|---|---|
| `permanent=false` *(default)* | the memory moves to `expired` and leaves the search index. It stops influencing answers but stays readable and auditable. **This is what "forget that" should do.** |
| `permanent=true` | the row and every edge touching it are erased. Irreversible. Reserve it for data that must not exist — a wrongly captured secret, or an erasure request |

```json
{ "memoryId": "mem_a1b2c3", "result": "expired", "permanent": false }
```

`result` is `expired` or `deleted`. A memory outside the requesting scope is
`404`, indistinguishable from one that does not exist.

---

## DELETE /v1/memories

Erase **everything** for one user: memories, relationships, audit trail and
jobs. Requires `?confirm=ERASE`.

```
DELETE /v1/memories?userId=user_123&confirm=ERASE
```

```json
{
  "deletedMemories": 42,
  "deletedRelations": 15,
  "deletedOperations": 88,
  "deletedJobs": 3
}
```

This is the right-to-erasure path, so the audit trail goes too: retaining "we
decided to store X about this user" after deleting X would defeat the point of
the request. Nothing is recorded about the purge itself, for the same reason.

The `confirm=ERASE` guard exists because a destructive collection-level
operation should not be reachable by getting a URL slightly wrong. Without it
the request is `422`.

---

## GET /v1/memories/{memoryId}

Query: `clientId`, `botId`, `phoneNumber` | `userId`.

```json
{
  "memory": { "...": "as above" },
  "relationships": [
    {
      "memoryId": "mem_x9y8z7",
      "relationType": "supersedes",
      "content": "User lives in Chennai.",
      "status": "superseded",
      "memoryType": "profile"
    }
  ],
  "sourceMemories": [],
  "derivedMemories": [
    {
      "memoryId": "pat_5f2a",
      "relationType": "supports",
      "content": "User has relocated within India twice in two years.",
      "status": "active",
      "memoryType": "pattern"
    }
  ],
  "supersededBy": null,
  "mergedInto": null
}
```

- `relationships` — outgoing edges (`supersedes`, `merged_from`, `derived_from`)
- `sourceMemories` — what this memory was built from
- `derivedMemories` — what rests on it, chiefly synthesised patterns

---

## GET /v1/memories/{memoryId}/history

Walks `supersedes` edges back to the original fact, newest first. Answers "when
did this change, and what did it say before?" — the reason superseded memories
are retained rather than deleted.

```json
{
  "memories": [
    { "memoryId": "mem_a1b2c3", "content": "User lives in Bangalore.", "status": "active",     "validFrom": "2026-07-01T00:00:00Z", "validUntil": null },
    { "memoryId": "mem_x9y8z7", "content": "User lives in Chennai.",   "status": "superseded", "validFrom": "2024-01-01T00:00:00Z", "validUntil": "2026-07-01T00:00:00Z" }
  ],
  "count": 2,
  "total": 2
}
```

---

## GET /v1/memories

Browse without a semantic query. Defaults to active only.

Query: scope, plus `status` (repeatable), `memoryType` (repeatable),
`limit` (≤ 200), `offset`.

```
GET /v1/memories?clientId=c&botId=b&phoneNumber=%2B91...&status=active&status=superseded&limit=50
```

---

## GET /v1/memories/{memoryId}/audit
## GET /v1/memories/audit/operations

Every Dream decision, newest first. **`NOOP`s are included, and are usually what
you want** — "why was nothing remembered?" is answered here rather than by
reading logs.

Query on the collection endpoint: scope, `action` (repeatable), `limit`,
`offset`.

```json
{
  "operations": [
    {
      "operationId": "op_44d1",
      "action": "SUPERSEDE",
      "memoryId": "mem_a1b2c3",
      "targetMemoryIds": ["mem_x9y8z7"],
      "reason": "new city Bangalore contradicts stored city Chennai",
      "inputContent": "I moved from Chennai to Bangalore last month.",
      "candidateMemoryIds": ["mem_x9y8z7", "mem_qq11"],
      "model": "azure-openai:gpt-4o-mini",
      "provider": "azure_openai",
      "confidence": 0.95,
      "validationNotes": [],
      "requestId": "req_7f3a",
      "timestamp": "2026-08-13T10:00:00Z"
    }
  ],
  "count": 1
}
```

`candidateMemoryIds` records exactly what the model was shown, so a questionable
decision can be replayed. `validationNotes` records every correction the backend
made to the model's proposal — dropped ids, clamped scores, refused targets — so
an overruled model is visible rather than silent.

---

## POST /v1/dream/synthesize

Queue a background synthesis run.

```json
{
  "phoneNumber": "+919999999999",
  "botId": "bot_123",
  "clientId": "client_456"
}
```

| Field | Default | Notes |
|---|---|---|
| `wait` | `false` | run inline and return the finished job. For tests, demos and CLI use — synthesis takes seconds, so it has no place on a user-facing path |
| `force` | `false` | re-evaluate every cluster. Idempotency still applies; this only widens candidate selection |

```json
{ "jobId": "dream_job_9a8b7c", "status": "queued" }
```

If a run is already queued or in flight for this user, **that** job is returned
rather than a second one created — re-running synthesis over unchanged evidence
spends tokens to reach the same NOOPs.

---

## GET /v1/dream/jobs/{jobId}

Query: scope. Required, deliberately — the specification sketch takes only a job
id, but a bare random id is not an authorisation check, and job records disclose
how much of a user is remembered.

```json
{
  "jobId": "dream_job_9a8b7c",
  "status": "completed",
  "patternsCreated": 3,
  "patternsUpdated": 1,
  "patternsSkipped": 2,
  "memoriesAnalyzed": 87,
  "clustersEvaluated": 6,
  "createdPatternIds": ["pat_5f2a", "pat_7c1d", "pat_8e3b"],
  "error": null,
  "createdAt": "2026-08-13T03:00:00Z",
  "completedAt": "2026-08-13T03:00:12Z",
  "durationMs": 12043.7
}
```

`status` is one of `queued`, `running`, `completed`, `failed`, `skipped`.
`skipped` with an `error` string is normal — most commonly the user has fewer
memories than `synthesis.min_memories`.

---

## GET /v1/dream/jobs

Query: scope, `limit` (≤ 100), `offset`.

## GET /v1/dream/stats

```json
{
  "counts": { "active": 42, "superseded": 8, "merged": 5, "expired": 0 },
  "patterns": 3,
  "actions": { "ADD": 44, "SUPERSEDE": 8, "MERGE": 5, "NOOP": 19, "SYNTHESIZE": 3 }
}
```

The action tallies are the first place to look when tuning thresholds: all
`NOOP` means the bars are too high, all `ADD` means consolidation is not firing.

---

## Operations

| | |
|---|---|
| `GET /health` | liveness. Does not touch dependencies, so an orchestrator will not restart the service because the database blipped |
| `GET /ready` | readiness. `503` if the canonical store or the vector store is down; the body still names which |
| `GET /v1/metrics` | counters and latency histograms as JSON |
| `GET /v1/metrics/tokens` | token usage split by operation, with an estimated cost |

`/ready` reports every dependency, not just the failing one:

```json
{
  "status": "degraded",
  "version": "0.1.0",
  "checks": {
    "database": { "ok": false, "provider": "mongodb", "error": "connection refused" },
    "vector_store": { "ok": true, "provider": "faiss" },
    "queue": { "ok": true, "provider": "database" },
    "embeddings": { "provider": "azure_openai", "model": "azure:text-embedding-3-small", "dimensions": 1536 },
    "llm": { "provider": "azure_openai", "model": "azure:gpt-4o-mini" }
  }
}
```

`embeddings` and `llm` are reported but are not hard dependencies: a provider
outage should not take the service out of rotation while reads still work.

`/v1/metrics` is deliberately not the Prometheus text format — with
`DREAM_OTEL_ENABLED=true` the same instruments export over OTLP, which is the
path meant for real scraping. This endpoint is for eyeballing a running instance.

Counters include `memories_added`, `memories_merged`, `memories_superseded`,
`patterns_created`, `patterns_updated`, `noop_decisions`, `llm_calls`,
`llm_tokens`, `embedding_calls`, `vector_searches`, `synthesis_runs`.
Histograms include `llm_latency_ms`, `embedding_latency_ms`,
`vector_search_latency_ms`, `retrieval_latency_ms`, `dream_latency_ms`,
`synthesis_duration_ms`.

`/v1/metrics/tokens` splits spend by operation, which is what tells you *where*
the tokens went rather than only how many there were:

```json
{
  "byOperation": [
    {
      "operation": "decide",
      "llmCalls": 3,
      "embeddingCalls": 0,
      "inputTokens": 88400,
      "outputTokens": 5900,
      "embeddingTokens": 900,
      "totalTokens": 95200,
      "estimatedCost": 0.016818
    },
    {
      "operation": "extract",
      "llmCalls": 3,
      "embeddingCalls": 0,
      "inputTokens": 41200,
      "outputTokens": 3100,
      "embeddingTokens": 0,
      "totalTokens": 44300,
      "estimatedCost": 0.00804
    }
  ],
  "totals": {
    "inputTokens": 129600,
    "outputTokens": 9000,
    "embeddingTokens": 900,
    "llmCalls": 6,
    "embeddingCalls": 0,
    "totalTokens": 139500,
    "estimatedCost": 0.024858
  },
  "pricing": {
    "currency": "USD",
    "llmInputPer1M": 0.15,
    "llmOutputPer1M": 0.6,
    "embeddingPer1M": 0.02,
    "note": "Estimates from configured rates, not billed amounts. ..."
  }
}
```

Input and output are reported separately because they are priced separately, and
`operation` is what makes "what does one `add` actually cost?" answerable at all.

Cost is **an estimate derived from the configured rates, never a billed
amount** — rates change, vary by region and commitment, and your deployment may
not use these models. Set `pricing.*` in `config/dream.yaml` to match your own
contract, or to `0` to switch cost reporting off.

These counters are **process-wide and in memory**: they reset on restart, and a
second replica keeps its own. For per-user accounting that survives restarts and
is correct across replicas, use `GET /v1/dream/stats`, which sums the token
usage recorded on each audit row.

---

## Memory object

| Field | Notes |
|---|---|
| `memoryId` | `mem_…`, or `pat_…` for synthesised patterns |
| `content` | third person, self-contained |
| `memoryType` | open vocabulary. Well-known: `profile`, `preference`, `goal`, `fact`, `event`, `relationship`, `constraint`, `skill`, `project`, `behavior`, `pattern`, `other` |
| `status` | `active`, `superseded`, `merged`, `expired` |
| `importance` `confidence` | 0–1 |
| `explicit` | `false` when inferred rather than stated. Patterns are always `false` |
| `temporalNature` | `permanent`, `current`, `temporary`, `historical`, `future`. `permanent` memories are protected from supersede |
| `createdAt` `updatedAt` | when we learned or last touched it |
| `validFrom` `validUntil` | when the *fact* is true — distinct from when we learned it |
| `supersededBy` `mergedInto` | what replaced it |
| `sourceMemoryIds` | provenance. Mandatory and non-empty on patterns |
| `sourceMessageIds` | caller-supplied message ids |
| `stale` | pattern flagged for re-evaluation because its evidence changed. Never auto-deleted |
| `metadata` | caller-supplied |
