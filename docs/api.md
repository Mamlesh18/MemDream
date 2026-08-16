# API reference

Base URL: `http://localhost:8000`. Interactive docs at `/docs`, OpenAPI schema
at `/openapi.json`.

---

## Conventions

**Everything is camelCase**, in both directions.

**Every memory endpoint is scoped.** Requests carry `clientId`, `botId`, and one
of `phoneNumber` or `userId`. This includes reads by id: a memory id is a random
handle, not an authorisation token, so possessing one is not enough to read the
record. A memory belonging to another tenant reports `404`, not `403` —
indistinguishable from not existing.

`POST` bodies carry scope as fields; `GET` endpoints take them as query
parameters.

**Authentication** is enabled by setting `DREAM_API_KEYS` to a comma-separated
list. Send the key as `X-API-Key`. Empty means no authentication — appropriate
when the service sits behind a gateway on a private network, which is why it is
opt-in rather than mandatory.

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
| `GET /health` | liveness. Does not touch dependencies, so an orchestrator will not restart the service because Mongo blipped |
| `GET /ready` | readiness. `503` if MongoDB or the vector store is down; the body still names which |
| `GET /v1/metrics` | counters and latency histograms as JSON |

`/v1/metrics` is deliberately not the Prometheus text format — with
`DREAM_OTEL_ENABLED=true` the same instruments export over OTLP, which is the
path meant for real scraping. This endpoint is for eyeballing a running instance.

Counters include `memories_added`, `memories_merged`, `memories_superseded`,
`patterns_created`, `patterns_updated`, `noop_decisions`, `llm_calls`,
`llm_tokens`, `embedding_calls`, `vector_searches`, `synthesis_runs`.
Histograms include `llm_latency_ms`, `embedding_latency_ms`,
`vector_search_latency_ms`, `retrieval_latency_ms`, `dream_latency_ms`,
`synthesis_duration_ms`.

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
