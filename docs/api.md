# REST API reference

Every MemDream capability over HTTP, usable from any language. Python users can
use the [SDK](sdk.md), which wraps each endpoint below with the same arguments.

New to MemDream? Read [Concepts](concepts.md) first.

---

## Contents

- [Basics](#basics)
- [Authentication](#authentication)
- [Scope](#scope)
- [Errors](#errors)
- [Limits](#limits)
- [Endpoint index](#endpoint-index)
- **Memories** — [remember](#post-v1memories) · [search](#post-v1memoriessearch) · [list](#get-v1memories) · [get](#get-v1memoriesmemoryid) · [history](#get-v1memoriesmemoryidhistory) · [create](#post-v1memoriesmanual) · [edit](#patch-v1memoriesmemoryid) · [forget](#delete-v1memoriesmemoryid) · [erase user](#delete-v1memories)
- **Audit** — [user log](#get-v1memoriesauditoperations) · [memory log](#get-v1memoriesmemoryidaudit)
- **Dream** — [synthesize](#post-v1dreamsynthesize) · [job](#get-v1dreamjobsjobid) · [jobs](#get-v1dreamjobs) · [stats](#get-v1dreamstats) · [consolidate](#post-v1dreamconsolidate)
- **Operations** — [health](#get-health) · [ready](#get-ready) · [metrics](#get-v1metrics) · [token usage](#get-v1metricstokens)
- **Objects** — [memory](#memory) · [decision result](#decision-result) · [audit operation](#audit-operation) · [dream job](#dream-job)

---

## Basics

| | |
|---|---|
| **Base URL** | wherever you run MemDream, e.g. `http://localhost:8000` — see [Self-hosting](self-hosting.md) |
| **Format** | JSON in, JSON out. `Content-Type: application/json` on bodies |
| **Field names** | `camelCase`, in both directions |
| **Unknown fields** | rejected with `422`. A misspelt field fails loudly instead of being ignored |
| **Timestamps** | ISO 8601. Responses are UTC, e.g. `2026-09-13T19:14:19.049000Z` |
| **Request id** | send `X-Request-ID` to use your own; every response returns one in the `X-Request-ID` header |
| **Interactive docs** | a running server serves `/docs` (Swagger UI), `/redoc` and `/openapi.json` |

---

## Authentication

Send your key in the `X-API-Key` header:

```bash
curl https://memory.example.com/v1/memories/search \
  -H "X-API-Key: sk_live_..." \
  -H "Content-Type: application/json" \
  -d '{"userId": "user_123", "query": "dietary restrictions"}'
```

A deployment runs in one of three modes, chosen by its operator:

| Mode | Configured by | Behaviour |
|---|---|---|
| **Open** | no keys configured | no authentication. Only for a server on a private network behind your own gateway |
| **Shared keys** | `DREAM_API_KEYS=key1,key2` | a valid key is required; `clientId` comes from the request |
| **Bound keys** | `DREAM_API_KEY_CLIENTS=key1:acme,key2:globex`, or the `api_keys` collection | the key determines the tenant. `clientId` may be omitted; a request naming a different client is refused |

**Use bound keys for any multi-tenant deployment.** With shared keys, any valid key
can name any `clientId`. Set `DREAM_API_KEY_REQUIRE_BINDING=true` to refuse
unbound keys entirely. Keys can be issued and revoked without a restart by
inserting documents into the `api_keys` collection:

```json
{ "_id": "key_acme_prod", "keyHash": "<sha256 hex of the key>", "clientId": "acme", "active": true }
```

Keys are compared as SHA-256 digests; the plaintext key is never stored.
Revocations take effect within 60 seconds.

```text
401  {"error": {"code": "unauthorized", "message": "Missing X-API-Key header"}}
401  {"error": {"code": "unauthorized", "message": "This API key is bound to a different client. Omit clientId, or send the one this key belongs to."}}
```

---

## Scope

Every memory endpoint runs inside a [scope](concepts.md#scope-whose-memory-is-it).

| Field | Required | |
|---|---|---|
| `userId` | one of these two | your identifier for the user. ≤ 128 characters |
| `phoneNumber` | | MemDream derives the user id from it. ≤ 32 characters |
| `agentId` | no | which agent or bot. ≤ 128 characters |
| `clientId` | no | the tenant. ≤ 128 characters. With a bound key, taken from the key |
| `namespace` | no | an object of string values, e.g. `{"env": "prod"}` |
| `botId` | no | previous name for `agentId`, still accepted |

**Where scope goes:**

- `POST` and `PATCH` — as fields in the JSON body.
- `GET` and `DELETE` — as query parameters. `namespace` is sent as JSON:
  `?userId=user_123&namespace={"env":"prod"}` (URL-encoded in practice). A value
  that is not a JSON object of strings is rejected with `422`.

A memory outside the requesting scope returns `404`, exactly as if it did not
exist.

---

## Errors

Every error has the same shape:

```json
{
  "error": {
    "code": "not_found",
    "message": "Memory mem_doesnotexist not found",
    "details": {}
  }
}
```

`code` is stable — branch on it rather than on `message`. `details` is present
when there is something useful to add.

| Status | `code` | Meaning |
|---|---|---|
| 401 | `unauthorized` | missing or invalid API key, or a bound key used with a different `clientId` |
| 403 | `tenant_isolation_violation` | an internal scope check failed. Should never occur; report it |
| 404 | `not_found` | the memory does not exist in this scope |
| 404 | `job_not_found` | the job does not exist in this scope |
| 409 | `invalid_state_transition` | the operation is not allowed in the memory's state — e.g. editing a `superseded` memory |
| 422 | `validation_error` | the request failed validation |
| 500 | `internal_error` | unexpected. `details.requestId` identifies the request in the server's logs |
| 500 | `configuration_error` | the server is misconfigured |
| 502 | `provider_error` | the LLM, embedding provider or vector store failed |
| 502 | `llm_response_error` | the model returned output that did not match the required schema |
| 503 | — | from [`GET /ready`](#get-ready) only: a required dependency is down |

Validation errors list every problem:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request failed validation",
    "details": {
      "errors": [
        {
          "loc": ["body"],
          "message": "Value error, Either userId or phoneNumber must be provided",
          "type": "value_error"
        }
      ]
    }
  }
}
```

---

## Limits

| Field | Limit |
|---|---|
| `prompt` | 1–20,000 characters |
| `query` | 1–8,000 characters |
| memory `content` | 1–4,000 characters |
| `memoryType` | ≤ 64 characters |
| `importance`, `confidence`, `minScore`, `decayImportanceFloor` | 0–1 |
| search `limit` | 1–100, default 10 |
| list and audit `limit` | 1–200, default 50 |
| jobs `limit` | 1–100, default 20 |
| `minMemories`, `compactMinMemories` | 2–1,000 |
| `decayAfterDays` | 1–3,650 |

MemDream does not rate-limit requests itself; apply limits at your gateway.

---

## Endpoint index

| Method | Path | |
|---|---|---|
| `POST` | [`/v1/memories`](#post-v1memories) | remember a message |
| `POST` | [`/v1/memories/search`](#post-v1memoriessearch) | search memories |
| `GET` | [`/v1/memories`](#get-v1memories) | list memories |
| `GET` | [`/v1/memories/{memoryId}`](#get-v1memoriesmemoryid) | get one memory with its relationships |
| `GET` | [`/v1/memories/{memoryId}/history`](#get-v1memoriesmemoryidhistory) | follow a memory's supersede chain |
| `POST` | [`/v1/memories/manual`](#post-v1memoriesmanual) | store a memory verbatim |
| `PATCH` | [`/v1/memories/{memoryId}`](#patch-v1memoriesmemoryid) | correct a memory |
| `DELETE` | [`/v1/memories/{memoryId}`](#delete-v1memoriesmemoryid) | forget or erase one memory |
| `DELETE` | [`/v1/memories`](#delete-v1memories) | erase everything for a user |
| `GET` | [`/v1/memories/audit/operations`](#get-v1memoriesauditoperations) | every decision for a user |
| `GET` | [`/v1/memories/{memoryId}/audit`](#get-v1memoriesmemoryidaudit) | every decision that touched a memory |
| `POST` | [`/v1/dream/synthesize`](#post-v1dreamsynthesize) | find patterns |
| `GET` | [`/v1/dream/jobs/{jobId}`](#get-v1dreamjobsjobid) | a synthesis job |
| `GET` | [`/v1/dream/jobs`](#get-v1dreamjobs) | a user's synthesis jobs |
| `GET` | [`/v1/dream/stats`](#get-v1dreamstats) | counts, decisions and cost for a user |
| `POST` | [`/v1/dream/consolidate`](#post-v1dreamconsolidate) | retire memories that no longer matter |
| `GET` | [`/health`](#get-health) | liveness |
| `GET` | [`/ready`](#get-ready) | readiness |
| `GET` | [`/v1/metrics`](#get-v1metrics) | counters and latencies |
| `GET` | [`/v1/metrics/tokens`](#get-v1metricstokens) | token spend by operation |

---

# Memories

## POST /v1/memories

**Remember a message.** MemDream extracts the durable facts in it and decides, for
each, whether to add, supersede, merge or ignore it. See
[what happens when you remember](concepts.md#what-happens-when-you-remember).

### Request body

| Field | Type | Default | |
|---|---|---|---|
| *scope* | | | [scope fields](#scope) |
| `prompt` | string | **required** | the user's message |
| `messageId` | string | — | your id for the message, stored on memories as `sourceMessageIds` |
| `timestamp` | datetime | now | when the message was sent. Anchors *"last month"*, *"tomorrow"* |
| `metadata` | object | `{}` | stored on the resulting memories, returned untouched |
| `raw` | boolean | `false` | store `prompt` verbatim as one memory instead of extracting facts from it. The add / supersede / merge decision still runs |

### Example

```bash
curl -X POST http://localhost:8000/v1/memories \
  -H "Content-Type: application/json" \
  -d '{
    "userId": "user_123",
    "prompt": "I'\''m allergic to peanuts and I always order the veg thali for lunch.",
    "messageId": "msg_0042",
    "metadata": {"channel": "whatsapp"}
  }'
```

```json
{
  "action": "ADD",
  "memoryId": "mem_11e64aec00a04cbb832a3a0f",
  "supersededMemoryIds": [],
  "mergedMemoryIds": [],
  "results": [
    {
      "action": "ADD",
      "memoryId": "mem_11e64aec00a04cbb832a3a0f",
      "content": "User is allergic to peanuts.",
      "reason": "User is allergic to peanuts.",
      "supersededMemoryIds": [],
      "mergedMemoryIds": [],
      "operationId": "op_c6d9e17b93894104b9d214e0"
    },
    {
      "action": "ADD",
      "memoryId": "mem_325cffcbbaa849cf93961a20",
      "content": "User always orders the veg thali for lunch.",
      "reason": "no related memory exists for this user",
      "supersededMemoryIds": [],
      "mergedMemoryIds": [],
      "operationId": "op_c3b9919987aa462e88d06548"
    }
  ],
  "extractedCount": 2,
  "requestId": "req_389aba4c69014a5b94e07a5f"
}
```

When a new fact contradicts an old one — here, *"I live in Bangalore now."*:

```json
{
  "action": "SUPERSEDE",
  "memoryId": "mem_7c41d2a09b8e4f3a9d6b1e20",
  "supersededMemoryIds": ["mem_002e9cd0a9d94445bb4edad2"],
  "mergedMemoryIds": [],
  "results": [
    {
      "action": "SUPERSEDE",
      "memoryId": "mem_7c41d2a09b8e4f3a9d6b1e20",
      "content": "User lives in Bangalore.",
      "reason": "User's address changed from Chennai to Bangalore.",
      "supersededMemoryIds": ["mem_002e9cd0a9d94445bb4edad2"],
      "mergedMemoryIds": [],
      "operationId": "op_5b0e8c1d7f2a4c6e9b3d1a70"
    }
  ],
  "extractedCount": 1,
  "requestId": "req_841696fd4e2e4e15a201e829"
}
```

Small talk — here, *"hey thanks!"* — is filtered before any model call and costs nothing:

```json
{
  "action": "NOOP",
  "memoryId": null,
  "supersededMemoryIds": [],
  "mergedMemoryIds": [],
  "results": [],
  "extractedCount": 0,
  "requestId": "req_01fd7b6a04ac427dba88bba7"
}
```

### Response

| Field | |
|---|---|
| `action` | `ADD`, `SUPERSEDE`, `MERGE` or `NOOP` — the most significant decision. A consolidation outranks an addition |
| `memoryId` | the memory that decision produced; `null` for `NOOP` |
| `supersededMemoryIds` `mergedMemoryIds` | what that decision replaced or absorbed |
| `results` | one [decision result](#decision-result) per extracted fact. Empty when nothing was extracted |
| `extractedCount` | facts found in the message |
| `requestId` | this request's id |

A `NOOP` with empty `results` means nothing worth keeping was found. The reason is
in the [audit log](#get-v1memoriesauditoperations).

Remembering can trigger [synthesis](#post-v1dreamsynthesize) in the background once
a user has accumulated enough new memories. It never delays or fails this response.

**SDK:** [`add()`](sdk.md#add)

---

## POST /v1/memories/search

**Search a user's memories**, [hybrid-ranked](concepts.md#how-recall-ranks-memories)
by meaning, importance, confidence, recency and status.

### Request body

| Field | Type | Default | |
|---|---|---|---|
| *scope* | | | [scope fields](#scope) |
| `query` | string | **required** | |
| `limit` | integer | `10` | 1–100 |
| `latestOnly` | boolean | `false` | only `active` memories — "what is true now?". Overrides the `include*` flags |
| `includeSuperseded` | boolean | `true` | include replaced facts, ranked below current ones |
| `includeMerged` | boolean | `false` | include memories absorbed into another |
| `includeExpired` | boolean | `false` | include retired memories |
| `memoryTypes` | string[] | all | e.g. `["preference", "constraint"]` |
| `minScore` | number | — | 0–1. Drop weaker matches |
| `asOf` | datetime | — | only memories whose validity window contains this instant — "what was true then?" |

### Example

```bash
curl -X POST http://localhost:8000/v1/memories/search \
  -H "Content-Type: application/json" \
  -d '{"userId": "user_123", "query": "where does the user live?", "limit": 5}'
```

```json
{
  "memories": [
    {
      "memoryId": "mem_002e9cd0a9d94445bb4edad2",
      "userId": "user_123",
      "content": "User lives in Chennai.",
      "memoryType": "fact",
      "status": "active",
      "importance": 0.8,
      "confidence": 0.9,
      "explicit": true,
      "temporalNature": "current",
      "createdAt": "2026-09-13T19:14:19.049000Z",
      "validFrom": "2026-09-13T00:00:00Z",
      "validUntil": null,
      "lastRecalledAt": null,
      "recallCount": 0,
      "metadata": {},
      "score": 0.703343,
      "scoreBreakdown": {
        "semanticSimilarity": 0.572239,
        "importance": 0.8,
        "confidence": 0.9,
        "recency": 1.0,
        "status": 1.0
      }
    },
    {
      "memoryId": "mem_11e64aec00a04cbb832a3a0f",
      "userId": "user_123",
      "content": "User is allergic to peanuts.",
      "memoryType": "fact",
      "status": "active",
      "importance": 0.8,
      "confidence": 0.9,
      "explicit": true,
      "temporalNature": "permanent",
      "createdAt": "2026-09-13T19:14:22.109000Z",
      "validFrom": "2026-09-13T19:14:22.109000Z",
      "validUntil": null,
      "lastRecalledAt": null,
      "recallCount": 0,
      "metadata": {"channel": "whatsapp"},
      "score": 0.540514,
      "scoreBreakdown": {
        "semanticSimilarity": 0.300857,
        "importance": 0.8,
        "confidence": 0.9,
        "recency": 1.0,
        "status": 1.0
      }
    }
  ],
  "count": 2,
  "requestId": "req_b41d8f3bfec64e539295c18a"
}
```

Memory objects in examples on this page are abbreviated; responses contain every
[memory field](#memory).

### Response

| Field | |
|---|---|
| `memories` | [memories](#memory), best first, each with `score` (0–1) and `scoreBreakdown` |
| `count` | number of results |
| `requestId` | |

`scoreBreakdown` holds the raw value of each ranking signal; `score` is their
weighted sum. Search only returns memories that clear a minimum similarity, so an
unrelated query returns an empty list.

Each search records which memories it returned, updating `lastRecalledAt` and
`recallCount`. That is the signal [consolidation](#post-v1dreamconsolidate) uses
to decide what is still useful. It does not change `updatedAt`.

**SDK:** [`search()`](sdk.md#search)

---

## GET /v1/memories

**List a user's memories** without a query, newest first.

### Query parameters

| Parameter | Default | |
|---|---|---|
| *scope* | | [scope parameters](#scope) |
| `status` | `active` | repeat to include several: `status=active&status=superseded`. Values: `active`, `superseded`, `merged`, `expired` |
| `memoryType` | all | repeat to include several |
| `limit` | `50` | 1–200 |
| `offset` | `0` | |

### Example

```bash
curl "http://localhost:8000/v1/memories?userId=user_123&status=active&status=superseded&limit=20"
```

```json
{
  "memories": [
    {
      "memoryId": "mem_325cffcbbaa849cf93961a20",
      "content": "User always orders the veg thali for lunch.",
      "memoryType": "preference",
      "status": "active",
      "importance": 0.5,
      "confidence": 0.9,
      "sourceMessageIds": ["msg_0042"],
      "metadata": {"channel": "whatsapp"}
    },
    {
      "memoryId": "mem_002e9cd0a9d94445bb4edad2",
      "content": "User lives in Chennai.",
      "memoryType": "fact",
      "status": "superseded",
      "importance": 0.8,
      "confidence": 0.9,
      "supersededBy": "mem_7c41d2a09b8e4f3a9d6b1e20",
      "metadata": {}
    }
  ],
  "count": 2,
  "total": 2,
  "offset": 0
}
```

### Response

| Field | |
|---|---|
| `memories` | [memories](#memory) |
| `count` | memories in this page |
| `total` | memories matching the filters, across all pages |
| `offset` | the offset used |

**SDK:** [`list()`](sdk.md#list)

---

## GET /v1/memories/{memoryId}

**Get one memory** with its relationships: what it replaced, what it was built
from, and what has been built on it.

### Query parameters

[Scope parameters](#scope).

### Example

```bash
curl "http://localhost:8000/v1/memories/mem_7c41d2a09b8e4f3a9d6b1e20?userId=user_123"
```

```json
{
  "memory": {
    "memoryId": "mem_7c41d2a09b8e4f3a9d6b1e20",
    "userId": "user_123",
    "content": "User lives in Bangalore.",
    "memoryType": "fact",
    "status": "active",
    "importance": 0.8,
    "confidence": 0.9,
    "temporalNature": "current",
    "supersededBy": null,
    "mergedInto": null,
    "sourceMemoryIds": [],
    "metadata": {}
  },
  "relationships": [
    {
      "memoryId": "mem_002e9cd0a9d94445bb4edad2",
      "relationType": "supersedes",
      "content": "User lives in Chennai.",
      "status": "superseded",
      "memoryType": "fact"
    }
  ],
  "sourceMemories": [],
  "derivedMemories": [],
  "supersededBy": null,
  "mergedInto": null
}
```

### Response

| Field | |
|---|---|
| `memory` | the [memory](#memory) |
| `relationships` | related memories, each with `memoryId`, `relationType`, `content`, `status`, `memoryType`. Relation types: `supersedes`, `superseded_by`, `merged_into`, `merged_from`, `derived_from`, `supports` |
| `sourceMemories` | for a pattern or summary: the memories it was built from |
| `derivedMemories` | patterns built on this memory |
| `supersededBy` `mergedInto` | the memory that replaced or absorbed this one |

`404 not_found` if the memory does not exist in this scope.

**SDK:** [`get()`](sdk.md#get)

---

## GET /v1/memories/{memoryId}/history

**Follow a memory's supersede chain** back to the original fact, newest first.
Answers *"when did this change, and what did it say before?"*

### Query parameters

[Scope parameters](#scope).

### Example

```bash
curl "http://localhost:8000/v1/memories/mem_7c41d2a09b8e4f3a9d6b1e20/history?userId=user_123"
```

```json
{
  "memories": [
    {
      "memoryId": "mem_7c41d2a09b8e4f3a9d6b1e20",
      "content": "User lives in Bangalore.",
      "status": "active",
      "createdAt": "2026-09-13T19:20:05.412000Z",
      "supersededBy": null
    },
    {
      "memoryId": "mem_002e9cd0a9d94445bb4edad2",
      "content": "User lives in Chennai.",
      "status": "superseded",
      "createdAt": "2026-09-13T19:14:19.049000Z",
      "supersededBy": "mem_7c41d2a09b8e4f3a9d6b1e20"
    }
  ],
  "count": 2,
  "total": 2,
  "offset": 0
}
```

A memory that never replaced anything returns a chain of one. `404 not_found` if
the memory does not exist in this scope.

**SDK:** [`history()`](sdk.md#history)

---

## POST /v1/memories/manual

**Store a memory exactly as written.** No extraction, no decision, no model call.
For importing facts you already know — a CRM profile, a signup form — or storing
something extraction would not keep.

### Request body

| Field | Type | Default | |
|---|---|---|---|
| *scope* | | | [scope fields](#scope) |
| `content` | string | **required** | write it in the third person, as extraction does: `"User is allergic to peanuts."` |
| `memoryType` | string | `"other"` | see [memory types](#memory) |
| `importance` | number | `0.6` | 0–1 |
| `confidence` | number | `1.0` | 0–1 |
| `explicit` | boolean | `true` | `false` marks it as inferred |
| `temporalNature` | string | `"current"` | `permanent`, `current`, `temporary`, `historical`, `future` |
| `validFrom` `validUntil` | datetime | — | when the fact is true. Consolidation retires it once `validUntil` passes |
| `metadata` | object | `{}` | |
| `allowDuplicate` | boolean | `false` | by default an identical active memory is returned instead of creating a second |

### Example

```bash
curl -X POST http://localhost:8000/v1/memories/manual \
  -H "Content-Type: application/json" \
  -d '{
    "userId": "user_123",
    "content": "User'\''s phone number ends in 4821.",
    "memoryType": "profile",
    "importance": 0.8
  }'
```

Status `201`:

```json
{
  "memory": {
    "memoryId": "mem_9eff9c243db54ffda6c75295",
    "userId": "user_123",
    "content": "User's phone number ends in 4821.",
    "memoryType": "profile",
    "status": "active",
    "importance": 0.8,
    "confidence": 1.0,
    "explicit": true,
    "temporalNature": "current",
    "createdAt": "2026-09-13T19:14:24.766000Z",
    "validFrom": "2026-09-13T19:14:24.766000Z",
    "validUntil": null,
    "metadata": {"source": "manual"}
  },
  "relationships": [],
  "sourceMemories": [],
  "derivedMemories": [],
  "supersededBy": null,
  "mergedInto": null
}
```

Returns the same shape as [`GET /v1/memories/{memoryId}`](#get-v1memoriesmemoryid).
`metadata.source` is set to `"manual"`.

**SDK:** [`create()`](sdk.md#create)

---

## PATCH /v1/memories/{memoryId}

**Correct a memory in place.** Only the fields you send change, and the memory
keeps its id.

Use this to fix a record — a mis-extracted value, a wrong importance. When the
user's situation actually changes, send the new fact to
[`POST /v1/memories`](#post-v1memories) instead, so the old one is kept as
history.

### Request body

[Scope fields](#scope), plus at least one of: `content`, `memoryType`,
`importance`, `confidence`, `explicit`, `temporalNature`, `validFrom`,
`validUntil`, `metadata`. `metadata` is merged into the existing metadata.

### Example

```bash
curl -X PATCH http://localhost:8000/v1/memories/mem_9eff9c243db54ffda6c75295 \
  -H "Content-Type: application/json" \
  -d '{"userId": "user_123", "importance": 0.9, "metadata": {"verified": true}}'
```

```json
{
  "memory": {
    "memoryId": "mem_9eff9c243db54ffda6c75295",
    "content": "User's phone number ends in 4821.",
    "memoryType": "profile",
    "status": "active",
    "importance": 0.9,
    "createdAt": "2026-09-13T19:14:24.766000Z",
    "updatedAt": "2026-09-13T19:14:24.770000Z",
    "metadata": {
      "source": "manual",
      "verified": true,
      "lastManualEdit": "2026-09-13T19:14:24.770000+00:00"
    }
  },
  "relationships": [],
  "sourceMemories": [],
  "derivedMemories": [],
  "supersededBy": null,
  "mergedInto": null
}
```

### Errors

| Status | |
|---|---|
| `404 not_found` | not in this scope |
| `409 invalid_state_transition` | the memory is `superseded` or `merged`. History is never rewritten |
| `422 validation_error` | no fields to change, or an invalid value |

**SDK:** [`update()`](sdk.md#update)

---

## DELETE /v1/memories/{memoryId}

**Forget a memory, or erase it.**

### Query parameters

| Parameter | Default | |
|---|---|---|
| *scope* | | [scope parameters](#scope) |
| `permanent` | `false` | `true` deletes the row and its relationships. Irreversible |

By default the memory moves to `expired`: it stops appearing in search but stays
auditable. This is what *"forget that"* should do. Reserve `permanent=true` for
data that must not exist, such as a wrongly captured secret.

### Example

```bash
curl -X DELETE "http://localhost:8000/v1/memories/mem_9eff9c243db54ffda6c75295?userId=user_123"
```

```json
{
  "memoryId": "mem_9eff9c243db54ffda6c75295",
  "result": "expired",
  "permanent": false,
  "operationId": null
}
```

With `permanent=true`, `result` is `"deleted"` and `permanent` is `true`.
`404 not_found` if the memory does not exist in this scope.

**SDK:** [`forget()`](sdk.md#forget)

---

## DELETE /v1/memories

**Erase everything for one user**: memories, relationships, audit trail and Dream
jobs. Irreversible. Intended for erasure requests — the audit trail goes too,
because keeping *"we decided to store X"* after deleting X would defeat the
request.

### Query parameters

| Parameter | |
|---|---|
| *scope* | [scope parameters](#scope) |
| `confirm` | **required**, must be exactly `ERASE` |

### Example

```bash
curl -X DELETE "http://localhost:8000/v1/memories?userId=user_123&confirm=ERASE"
```

```json
{
  "deletedMemories": 5,
  "deletedRelations": 0,
  "deletedOperations": 9,
  "deletedJobs": 1,
  "operationId": null
}
```

Without `confirm=ERASE`:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Erasing a user's entire memory requires ?confirm=ERASE",
    "details": {"userId": "user_123"}
  }
}
```

Only the requested scope is erased: `userId=user_123` with no `agentId` does not
touch `user_123`'s memories under an agent.

**SDK:** [`purge()`](sdk.md#purge)

---

# Audit

Every decision MemDream makes is recorded — `NOOP`s included — with the input,
the reason, the memories the model was shown and the tokens it used. It answers
*"why was this remembered?"* and *"why wasn't it?"*.

## GET /v1/memories/audit/operations

**Every decision for a user**, newest first.

### Query parameters

| Parameter | Default | |
|---|---|---|
| *scope* | | [scope parameters](#scope) |
| `action` | all | repeat to include several: `action=NOOP&action=SUPERSEDE` |
| `limit` | `50` | 1–200 |
| `offset` | `0` | |

### Example

```bash
curl "http://localhost:8000/v1/memories/audit/operations?userId=user_123&action=NOOP&limit=2"
```

```json
{
  "operations": [
    {
      "operationId": "op_9405b0eb19874f459f26e0bf",
      "userId": "user_123",
      "agentId": null,
      "clientId": null,
      "namespace": {},
      "scopeKey": "9dc7ad5b46771f7e11d973ecee453a23",
      "action": "NOOP",
      "memoryId": null,
      "targetMemoryIds": [],
      "reason": "greeting or acknowledgement",
      "inputContent": "hey thanks!",
      "candidateMemoryIds": [],
      "model": null,
      "provider": "triage",
      "confidence": null,
      "requestId": "req_01fd7b6a04ac427dba88bba7",
      "jobId": null,
      "latencyMs": 0.0,
      "tokenUsage": {},
      "validationNotes": [],
      "timestamp": "2026-09-13T19:14:23.101000Z"
    },
    {
      "operationId": "op_dbf9d05e6f2b45389db84b04",
      "userId": "user_123",
      "agentId": null,
      "clientId": null,
      "namespace": {},
      "scopeKey": "9dc7ad5b46771f7e11d973ecee453a23",
      "action": "NOOP",
      "memoryId": null,
      "targetMemoryIds": [],
      "reason": "No relevant user information to extract.",
      "inputContent": "What time do you close?",
      "candidateMemoryIds": [],
      "model": "azure_openai:gpt-4o-mini",
      "provider": null,
      "confidence": null,
      "requestId": "req_841696fd4e2e4e15a201e829",
      "jobId": null,
      "latencyMs": 671.04,
      "tokenUsage": {"prompt_tokens": 453, "completion_tokens": 20, "total_tokens": 473},
      "validationNotes": [],
      "timestamp": "2026-09-13T19:14:23.099000Z"
    }
  ],
  "count": 2
}
```

Each entry is an [audit operation](#audit-operation).

**SDK:** [`audit()`](sdk.md#audit)

---

## GET /v1/memories/{memoryId}/audit

**Every decision that touched one memory**, newest first.

### Query parameters

| Parameter | Default | |
|---|---|---|
| *scope* | | [scope parameters](#scope) |
| `limit` | `50` | 1–200 |

### Example

```bash
curl "http://localhost:8000/v1/memories/mem_caf4e1adc96f461f8042a9a9/audit?userId=user_123"
```

```json
{
  "operations": [
    {
      "operationId": "op_7a90e257a8ee45a7b4fbf54a",
      "userId": "user_123",
      "action": "ADD",
      "memoryId": "mem_caf4e1adc96f461f8042a9a9",
      "targetMemoryIds": [],
      "reason": "no related memory exists for this user",
      "inputContent": "User prefers delivery after 7pm.",
      "candidateMemoryIds": [],
      "model": "azure_openai:gpt-4o-mini",
      "provider": "azure_openai",
      "confidence": 1.0,
      "requestId": "req_2965a35167ac4cca94afdc5b",
      "jobId": null,
      "latencyMs": null,
      "tokenUsage": {},
      "validationNotes": [],
      "timestamp": "2026-09-13T19:14:23.641000Z"
    }
  ],
  "count": 1
}
```

`404 not_found` if the memory does not exist in this scope.

**SDK:** [`audit(memory_id=...)`](sdk.md#audit)

---

# Dream

## POST /v1/dream/synthesize

**Find patterns** across a user's memories. See
[synthesis](concepts.md#synthesis--finding-patterns).

Returns immediately with a job id; the work runs in the background. If a run is
already queued or running for this user, that job is returned instead of a new
one. Synthesis also runs automatically as users accumulate memories, so most
integrations never call this.

### Request body

| Field | Type | Default | |
|---|---|---|---|
| *scope* | | | [scope fields](#scope) |
| `wait` | boolean | `false` | run synthesis before responding and return the finished job. For scripts and tests — synthesis can take seconds |
| `force` | boolean | `false` | re-evaluate groups that already produced a pattern |
| `minMemories` | integer | server default (20) | active memories needed before looking, for this run only. 2–1,000 |

### Example

```bash
curl -X POST http://localhost:8000/v1/dream/synthesize \
  -H "Content-Type: application/json" \
  -d '{"userId": "user_123"}'
```

```json
{
  "jobId": "dream_job_cb7dc585344c4eb5",
  "status": "queued",
  "patternsCreated": null,
  "memoriesAnalyzed": null
}
```

With `"wait": true`:

```json
{
  "jobId": "dream_job_cb7dc585344c4eb5",
  "status": "completed",
  "patternsCreated": 1,
  "memoriesAnalyzed": 21
}
```

### Response

| Field | |
|---|---|
| `jobId` | poll it with [`GET /v1/dream/jobs/{jobId}`](#get-v1dreamjobsjobid) |
| `status` | `queued`, `running`, `completed`, `failed` or `skipped` |
| `patternsCreated` `memoriesAnalyzed` | only when `wait` is `true`; otherwise `null` |

A job can complete with no patterns: the user may have too few active memories,
or no group may be similar enough. That is expected — a pattern drawn from a
handful of loosely related memories would be coincidence, not insight.

**SDK:** [`synthesize()`](sdk.md#synthesize)

---

## GET /v1/dream/jobs/{jobId}

**A synthesis job's status and result.**

### Query parameters

[Scope parameters](#scope). A job belonging to another scope returns
`404 job_not_found`.

### Example

```bash
curl "http://localhost:8000/v1/dream/jobs/dream_job_cb7dc585344c4eb5?userId=user_123"
```

```json
{
  "jobId": "dream_job_cb7dc585344c4eb5",
  "status": "completed",
  "patternsCreated": 1,
  "patternsUpdated": 0,
  "patternsSkipped": 0,
  "memoriesAnalyzed": 21,
  "clustersEvaluated": 3,
  "createdPatternIds": ["pat_4f1c9e2a7b3d4e5f8a6c0b12"],
  "error": null,
  "createdAt": "2026-09-13T19:14:24.789000Z",
  "completedAt": "2026-09-13T19:14:31.201000Z",
  "durationMs": 6412.0
}
```

### Response

| Field | |
|---|---|
| `status` | `queued`, `running`, `completed`, `failed`, `skipped` |
| `memoriesAnalyzed` | active memories considered |
| `clustersEvaluated` | groups of related memories the model examined |
| `patternsCreated` `patternsUpdated` `patternsSkipped` | outcomes. Skipped groups already had an equivalent pattern |
| `createdPatternIds` | fetch them with [`GET /v1/memories/{memoryId}`](#get-v1memoriesmemoryid) |
| `error` | set when `status` is `failed` |
| `createdAt` `completedAt` `durationMs` | |

**SDK:** [`get_job()`](sdk.md#get_job), [`wait_for()`](sdk.md#wait_for)

---

## GET /v1/dream/jobs

**A user's synthesis jobs**, newest first.

### Query parameters

| Parameter | Default | |
|---|---|---|
| *scope* | | [scope parameters](#scope) |
| `limit` | `20` | 1–100 |
| `offset` | `0` | |

### Example

```bash
curl "http://localhost:8000/v1/dream/jobs?userId=user_123"
```

```json
{
  "jobs": [
    {
      "jobId": "dream_job_cb7dc585344c4eb5",
      "userId": "user_123",
      "agentId": null,
      "clientId": null,
      "namespace": {},
      "scopeKey": "9dc7ad5b46771f7e11d973ecee453a23",
      "status": "completed",
      "jobType": "synthesis",
      "memoriesAnalyzed": 21,
      "clustersEvaluated": 3,
      "patternsCreated": 1,
      "patternsUpdated": 0,
      "patternsSkipped": 0,
      "noopDecisions": 2,
      "error": null,
      "createdPatternIds": ["pat_4f1c9e2a7b3d4e5f8a6c0b12"],
      "createdAt": "2026-09-13T19:14:24.789000Z",
      "startedAt": "2026-09-13T19:14:24.789000Z",
      "completedAt": "2026-09-13T19:14:31.201000Z",
      "durationMs": 6412.0,
      "requestedBy": null,
      "metadata": {"force": false, "minMemories": 3}
    }
  ],
  "count": 1
}
```

Each entry is a full [dream job](#dream-job), with more fields than
[`GET /v1/dream/jobs/{jobId}`](#get-v1dreamjobsjobid) returns.

**SDK:** [`list_jobs()`](sdk.md#list_jobs)

---

## GET /v1/dream/stats

**Counts, decisions and cost for one user.** Durable — computed from stored data,
so correct across restarts and replicas.

### Query parameters

[Scope parameters](#scope).

### Example

```bash
curl "http://localhost:8000/v1/dream/stats?userId=user_123"
```

```json
{
  "counts": {"active": 4, "superseded": 0, "merged": 0, "expired": 1},
  "patterns": 0,
  "actions": {"ADD": 4, "EXPIRE": 1, "MANUAL_ADD": 1, "MANUAL_UPDATE": 1, "NOOP": 2},
  "tokens": {
    "byAction": {
      "ADD": {"operations": 4, "inputTokens": 603, "outputTokens": 71},
      "EXPIRE": {"operations": 1, "inputTokens": 0, "outputTokens": 0},
      "MANUAL_ADD": {"operations": 1, "inputTokens": 0, "outputTokens": 0},
      "MANUAL_UPDATE": {"operations": 1, "inputTokens": 0, "outputTokens": 0},
      "NOOP": {"operations": 2, "inputTokens": 453, "outputTokens": 20}
    },
    "totals": {
      "operations": 9,
      "inputTokens": 1056,
      "outputTokens": 91,
      "totalTokens": 1147,
      "estimatedCost": 0.000213
    },
    "currency": "USD"
  }
}
```

### Response

| Field | |
|---|---|
| `counts` | memories by status |
| `patterns` | active patterns |
| `actions` | decisions taken, by [audit action](#audit-operation) |
| `tokens.byAction` | LLM tokens spent, by action |
| `tokens.totals.estimatedCost` | estimated from the server's configured prices — an estimate, not a bill |

The `actions` tally is the first place to look when tuning: mostly `NOOP` means
extraction is too strict for your messages; only `ADD` means supersede and merge
are not firing.

**SDK:** [`stats()`](sdk.md#stats)

---

## POST /v1/dream/consolidate

**Retire memories that no longer earn their place**, for one user — the same pass
the server runs for every user each night. See
[consolidation](concepts.md#consolidation--forgetting-safely).

**A dry run by default.** Without `"dryRun": false` it reports what it would do
and changes nothing. Retiring moves memories to `expired`, and compaction to
`merged`; nothing is deleted.

### Request body

| Field | Type | Default | |
|---|---|---|---|
| *scope* | | | [scope fields](#scope) |
| `dryRun` | boolean | **`true`** | `false` to apply |
| `compact` | boolean | `false` | also fold crowds of near-identical, low-value memories into summaries. One model call per group |
| `compactMinMemories` | integer | server default (40) | eligible memories needed before compaction runs. 2–1,000 |
| `decay` | boolean | server default (off) | apply the recall-decay rule in this run |
| `decayAfterDays` | integer | server default (90) | not recalled for this many days makes a memory a candidate. 1–3,650 |
| `decayImportanceFloor` | number | server default (0.5) | memories at or above this importance are never decayed |
| `recallEpoch` | date | server default | the date recall tracking began, e.g. `"2026-01-01"`. **Decay cannot fire without one** |

Every override applies to this request only and never changes the nightly pass.

The three rules:

| Rule | Retires | Runs |
|---|---|---|
| lapsed | memories whose `validUntil` has passed | always |
| decay | memories not recalled within `decayAfterDays`, below `decayImportanceFloor` | with `decay` and a `recallEpoch` |
| compaction | groups of similar low-importance memories, replaced by one summary | with `compact` |

None of them touches `permanent` memories, memories an active pattern rests on, or
recent memories.

### Example: measure first

```bash
curl -X POST http://localhost:8000/v1/dream/consolidate \
  -H "Content-Type: application/json" \
  -d '{"userId": "user_123", "decay": true, "decayAfterDays": 30, "recallEpoch": "2026-01-01"}'
```

```json
{
  "dryRun": true,
  "scanned": 65,
  "lapsed": 2,
  "decayed": 54,
  "expired": 0,
  "protected": 4,
  "expiredIds": [],
  "clusters": 0,
  "compacted": 0,
  "absorbed": 0,
  "rejected": 0,
  "decayApplied": true,
  "durationMs": 97.95
}
```

### Example: apply, with compaction

```bash
curl -X POST http://localhost:8000/v1/dream/consolidate \
  -H "Content-Type: application/json" \
  -d '{"userId": "user_123", "dryRun": false, "compact": true}'
```

```json
{
  "dryRun": false,
  "scanned": 65,
  "lapsed": 2,
  "decayed": 0,
  "expired": 2,
  "protected": 0,
  "expiredIds": ["mem_c86671721476405597a535a2", "mem_3f882044fbc64983a8693089"],
  "clusters": 5,
  "compacted": 5,
  "absorbed": 50,
  "rejected": 0,
  "decayApplied": false,
  "durationMs": 16440.53
}
```

### Response

| Field | |
|---|---|
| `dryRun` | whether anything was changed |
| `scanned` | active memories examined |
| `lapsed` | memories whose validity window has closed |
| `decayed` | decay candidates. Reported even when decay was not applied — that is how you choose a window |
| `expired` | memories actually retired. Always `0` in a dry run |
| `protected` | candidates kept because an active pattern rests on them |
| `expiredIds` | the first 20 retired ids |
| `clusters` | compaction: groups found |
| `compacted` | compaction: summaries written |
| `absorbed` | compaction: memories folded into summaries |
| `rejected` | compaction: summaries refused — the model declined, or the summary looked lossy |
| `decayApplied` | whether decay was in force for this run |
| `durationMs` | server time |

In a dry run, applying would retire `lapsed`, plus `decayed` when `decayApplied`
is `true`.

**SDK:** [`consolidate()`](sdk.md#consolidate)

---

# Operations

These endpoints are not scoped and are meant for infrastructure: load balancers,
orchestrators and dashboards.

## GET /health

**Liveness.** Returns `200` whenever the process is up. Touches no dependency, so
an orchestrator will not restart the service because the database blipped.

```json
{"status": "ok", "version": "0.1.0", "checks": {}}
```

## GET /ready

**Readiness**, with a check for every dependency.

```json
{
  "status": "ok",
  "version": "0.1.0",
  "checks": {
    "database": {"ok": true, "provider": "mongodb"},
    "vector_store": {"ok": true, "provider": "faiss"},
    "queue": {"ok": true, "provider": "inline", "depth": 0},
    "embeddings": {"provider": "azure_openai", "model": "azure_openai:text-embedding-3-small", "dimensions": 1536},
    "llm": {"provider": "azure_openai", "model": "azure_openai:gpt-4o-mini"}
  }
}
```

| `status` | HTTP | Meaning |
|---|---|---|
| `ok` | 200 | everything is answering |
| `degraded` | 200 | the job queue is down: background synthesis is affected, reads and writes are not. Keep the instance in rotation |
| `degraded` | 503 | the database or vector store is down. Take the instance out of rotation |

A failing check carries `"ok": false` and an `error`.

## GET /v1/metrics

**Counters and latency histograms** for this process, as JSON, plus the same
token report as [`/v1/metrics/tokens`](#get-v1metricstokens). Keys: `counters`,
`histograms`, `tokens`.

```json
{
  "counters": {
    "llm_calls": {
      "operation=extract,provider=azure_openai": 3.0,
      "operation=decide,provider=azure_openai": 1.0
    },
    "embedding_calls": {
      "operation=remember,provider=azure_openai": 4.0,
      "operation=search,provider=azure_openai": 2.0
    }
  },
  "histograms": {"...": "..."},
  "tokens": {"...": "..."}
}
```

In-memory and per process: counters reset on restart, and each replica keeps its
own. For production monitoring, enable OpenTelemetry export on the server
(`DREAM_OTEL_ENABLED=true`).

## GET /v1/metrics/tokens

**Token spend by operation** for this process, with an estimated cost.

```json
{
  "byOperation": [
    {
      "operation": "decide",
      "llmCalls": 1,
      "embeddingCalls": 0,
      "inputTokens": 603,
      "outputTokens": 71,
      "embeddingTokens": 0,
      "totalTokens": 674,
      "estimatedCost": 0.000133
    },
    {
      "operation": "extract",
      "llmCalls": 3,
      "embeddingCalls": 0,
      "inputTokens": 1374,
      "outputTokens": 232,
      "embeddingTokens": 0,
      "totalTokens": 1606,
      "estimatedCost": 0.000345
    },
    {
      "operation": "search",
      "llmCalls": 0,
      "embeddingCalls": 2,
      "inputTokens": 0,
      "outputTokens": 0,
      "embeddingTokens": 12,
      "totalTokens": 12,
      "estimatedCost": 0.0
    }
  ],
  "totals": {
    "inputTokens": 1977,
    "outputTokens": 303,
    "embeddingTokens": 51,
    "llmCalls": 4,
    "embeddingCalls": 7,
    "totalTokens": 2331,
    "estimatedCost": 0.000479
  },
  "pricing": {
    "currency": "USD",
    "llmInputPer1M": 0.15,
    "llmOutputPer1M": 0.6,
    "embeddingPer1M": 0.02,
    "note": "Estimates from configured rates, not billed amounts. Set them under `pricing` in config/dream.yaml to match your deployment and region."
  }
}
```

Operations: `extract` and `decide` (remembering), `remember` (embedding new
memories), `search`, `manual`, `synthesize`. Process-wide and in-memory, like
[`/v1/metrics`](#get-v1metrics); for durable per-user cost use
[`GET /v1/dream/stats`](#get-v1dreamstats).

---

# Objects

## Memory

Returned by search, list, get, history, create and edit. The full field reference
is in [Concepts → the memory object](concepts.md#the-memory-object).

| Field | Type | |
|---|---|---|
| `memoryId` | string | `mem_…`, or `pat_…` for a pattern |
| `userId` `agentId` `clientId` `namespace` `scopeKey` `phoneNumberHash` | | the scope |
| `content` | string | |
| `memoryType` | string | `profile`, `preference`, `goal`, `fact`, `event`, `relationship`, `constraint`, `skill`, `project`, `behavior`, `pattern`, `other`, or your own |
| `status` | string | `active`, `superseded`, `merged`, `expired` |
| `importance` `confidence` | number | 0–1 |
| `explicit` | boolean | stated rather than inferred |
| `temporalNature` | string | `permanent`, `current`, `temporary`, `historical`, `future` |
| `createdAt` `updatedAt` | datetime | |
| `validFrom` `validUntil` | datetime \| null | |
| `supersededBy` `mergedInto` | string \| null | |
| `sourceMemoryIds` `sourceMessageIds` | string[] | |
| `embeddingModel` `embeddingDim` | | the vector itself is never returned |
| `contentHash` | string | |
| `lastRecalledAt` | datetime \| null | |
| `recallCount` | integer | |
| `stale` | boolean | a pattern whose evidence changed |
| `lastSynthesizedAt` | datetime \| null | |
| `metadata` | object | |
| `score` `scoreBreakdown` | | search results only |

## Decision result

One entry in `results` from [`POST /v1/memories`](#post-v1memories).

| Field | |
|---|---|
| `action` | `ADD`, `SUPERSEDE`, `MERGE`, `NOOP` |
| `memoryId` | the resulting memory; `null` for `NOOP` |
| `content` | the fact as stored |
| `reason` | the model's explanation, or the rule that decided |
| `supersededMemoryIds` `mergedMemoryIds` | what it replaced or absorbed |
| `operationId` | the matching [audit operation](#audit-operation) |

## Audit operation

| Field | |
|---|---|
| `operationId` | `op_…` |
| `userId` `agentId` `clientId` `namespace` `scopeKey` | scope |
| `action` | see below |
| `memoryId` | the memory that ended up active as a result; `null` for `NOOP` |
| `targetMemoryIds` | memories superseded, merged or expired by this operation |
| `reason` | why |
| `inputContent` | the input that led to the decision |
| `candidateMemoryIds` | the existing memories the model was shown |
| `model` `provider` | which model decided. `provider: "triage"` means small talk was filtered with no model call |
| `confidence` | |
| `requestId` `jobId` | what triggered it |
| `latencyMs` | |
| `tokenUsage` | `prompt_tokens`, `completion_tokens`, `total_tokens`, when a model was called |
| `validationNotes` | set when MemDream overruled or trimmed the model's proposal |
| `timestamp` | |

| `action` | |
|---|---|
| `ADD` `SUPERSEDE` `MERGE` `NOOP` | decisions on a remembered message |
| `MANUAL_ADD` `MANUAL_NOOP` `MANUAL_UPDATE` | [manual create](#post-v1memoriesmanual) (`MANUAL_NOOP`: an identical memory already existed) and [edit](#patch-v1memoriesmemoryid) |
| `EXPIRE` | a memory retired — forgotten on request or by consolidation |
| `DELETE` | a memory permanently erased |
| `SYNTHESIZE` `SYNTHESIZE_UPDATE` | a pattern created or updated by synthesis |

## Dream job

| Field | |
|---|---|
| `jobId` | `dream_job_…` |
| `userId` `agentId` `clientId` `namespace` `scopeKey` | scope |
| `status` | `queued`, `running`, `completed`, `failed`, `skipped` |
| `jobType` | `synthesis` |
| `memoriesAnalyzed` `clustersEvaluated` | |
| `patternsCreated` `patternsUpdated` `patternsSkipped` `noopDecisions` | |
| `createdPatternIds` | |
| `error` | |
| `createdAt` `startedAt` `completedAt` `durationMs` | |
| `requestedBy` | |
| `metadata` | options the run used, and a `consolidation` summary when the pre-synthesis consolidation pass ran |
