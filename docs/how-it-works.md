# How it works, technically

A walkthrough of the actual code paths, with file references you can follow.

For *why* each decision went the way it did, see [`architecture.md`](architecture.md).
This document is the *what happens*.

---

## Contents

1. [The 30-second version](#1-the-30-second-version)
2. [Anatomy of one request](#2-anatomy-of-one-request)
3. [Storage](#3-storage)
4. [Tenant isolation](#4-tenant-isolation)
5. [Retrieval](#5-retrieval)
6. [The Dream engine](#6-the-dream-engine)
7. [Validation — the safety boundary](#7-validation--the-safety-boundary)
8. [Lifecycle transitions](#8-lifecycle-transitions)
9. [Synthesis](#9-synthesis)
10. [Manual management](#10-manual-management)
11. [Failure behaviour](#11-failure-behaviour)
12. [Provider abstraction](#12-provider-abstraction)
13. [Where to change things](#13-where-to-change-things)

---

## 1. The 30-second version

Two processes, four stores, one write path.

```
                     ┌──────────────────────────────────┐
   POST /v1/memories │            API (uvicorn)         │
   ─────────────────►│  extract → decide → transition   │
                     └───────┬──────────────────┬───────┘
                             │                  │
                    ┌────────▼───────┐  ┌───────▼────────┐
                    │    MongoDB     │  │     FAISS      │
                    │ source of truth│  │ semantic index │
                    │ memories       │  │ one partition  │
                    │ relations      │  │ per user       │
                    │ dream_operations│ └────────────────┘
                    │ dream_jobs     │
                    └────────┬───────┘
                             │ job id via Redis
                     ┌───────▼──────────────────────────┐
                     │        Worker (arq)              │
                     │  select → cluster → synthesise   │
                     └──────────────────────────────────┘
```

MongoDB is authoritative and holds every embedding. FAISS is a derived index that
can be deleted and rebuilt (`scripts/rebuild_index.py`). Redis carries only job
ids — job *state* lives in Mongo, so status is answerable whatever the broker is.

---

## 2. Anatomy of one request

`POST /v1/memories` with `{"prompt": "I moved to Bangalore last month.", ...}`.

### 2.1 Middleware and scope

`app/main.py:94` assigns a request id and binds it to a context variable, so
every log line for this request carries it without threading a logger through
the call stack.

`app/api/deps.py:scope_from_request` builds the `MemoryScope` — the single
`(clientId, botId, userId)` triple everything downstream is bound to. The phone
number is hashed here and discarded (§4).

### 2.2 Extraction

`app/memory/service.py:92` → `app/memory/extractor.py:extract`.

One LLM call, `EXTRACTION_SYSTEM` (`app/dream/prompts.py:28`). The user message
is sent as a **JSON payload, not prose** — instructions live in the system
prompt, data in the user message, so a memory reading "ignore previous
instructions" is data.

Returns `ExtractionResult` — validated Pydantic, never free text. One message can
yield several facts:

```
"I live in Chennai and I work at Acme as a backend engineer."
  → "User lives in Chennai."                          (profile)
  → "User works at Acme as a backend engineer."       (profile)
```

### 2.3 Policy

`app/core/policy.py:MemoryPolicy.filter` — pure code, no model. Applies
confidence and importance floors, type allow/block lists, the sensitivity rule,
and a per-prompt cap. Rejections are *returned*, not dropped, so the reason
reaches the audit log.

This is where a peanut allergy gets discarded under the default
`allow_sensitive: false`, and why `POST /v1/memories/manual` exists (§10).

### 2.4 Per-candidate loop

`app/memory/service.py:127` — **sequential, not concurrent**. Each candidate is
reasoned about against the state the previous one left behind. Running them in
parallel would let a message stating the same fact twice produce two active
memories, because both calls would see an empty candidate set.

Each candidate goes through `DreamEngine.process` (`app/dream/engine.py:92`).

### 2.5 The engine pipeline

`app/dream/engine.py:126` (`_process`), in order:

| Step | Code | Cost |
|---|---|---|
| Exact-duplicate check | `merge.py:find_exact_duplicate` | 1 indexed Mongo query |
| Embed | `embeddings.embed` | 1 embedding call |
| Candidate retrieval | `retriever.py:184` | 1 Mongo + 1 FAISS + 1 Mongo |
| Decision | `engine.py:272` | 1 LLM call |
| Validation | `validation.py:47` | pure code |
| Transition | `lifecycle.py` | 2–4 Mongo writes + index update |
| Audit | `audit_repository.py` | 1 Mongo insert |

The cheap deterministic checks come first. An exact repeat returns `NOOP` at step
one having spent no embedding and no LLM call — measurably, ~40 ms instead of
~2600 ms.

If no related memories exist (`engine.py:168`), it short-circuits to `ADD`
without a decision call at all.

### 2.6 Response

```json
{
  "action": "SUPERSEDE",
  "memoryId": "mem_a1b2",
  "supersededMemoryIds": ["mem_x9y8"],
  "results": [ { "action": "...", "reason": "...", "..." : "..." } ],
  "extractedCount": 1
}
```

`results` is the complete answer — one per extracted fact. The flat fields mirror
the most significant one (`service.py:41`: supersede > merge > add > noop), so
the common single-fact case reads simply.

---

## 3. Storage

### 3.1 Collections

`app/repositories/mongo.py`

| Collection | Holds |
|---|---|
| `memories` | canonical rows, embeddings included |
| `memory_relations` | edges, written in both directions |
| `dream_operations` | audit log, one row per decision |
| `dream_jobs` | synthesis job state |

Indexes (`mongo.py:ensure_indexes`) all lead with the scope triple:

```
scope_status        (clientId, botId, userId, status)      ← the workhorse
scope_type_status   (clientId, botId, userId, memoryType, status)
scope_content_hash  (clientId, botId, userId, contentHash) ← exact-duplicate path
scope_validity      (clientId, botId, userId, validFrom, validUntil)
source_memory_ids   (sourceMemoryIds)                      ← pattern staleness
```

### 3.2 The memory document

`app/models/memory.py:Memory`. Fields that carry real semantics:

| Field | Meaning |
|---|---|
| `status` | `active` · `superseded` · `merged` · `expired` |
| `explicit` | `false` = inferred. Patterns are always `false` |
| `temporalNature` | `permanent` facts are protected from supersede |
| `validFrom` / `validUntil` | when the **fact** is true — distinct from `createdAt`, when we *learned* it |
| `supersededBy` / `mergedInto` | what replaced it |
| `sourceMemoryIds` | provenance; mandatory and non-empty on patterns |
| `contentHash` | normalised-text hash driving exact-duplicate detection |
| `stale` | pattern flagged for re-evaluation; never auto-deleted |

`validFrom` vs `createdAt` is the pair that makes time travel work: "user moved
to Bangalore in July" learned in August has `validFrom` July, `createdAt` August.

### 3.3 FAISS

`app/providers/vector_store/faiss_store.py`

- **One index file per scope.** Filename is `sha256(clientId/botId/userId)[:40]`
  (`faiss_store.py:110`) — hashed so tenant ids never touch the filesystem.
- `IndexIDMap2` over `IndexFlatIP` with L2-normalised vectors = **exact** cosine.
  Per-user counts are small enough that approximation buys nothing.
- FAISS addresses vectors by int64; the string↔int mapping lives in a JSON
  sidecar written atomically alongside the index.
- Writes: `FileLock` → mutate → temp file → `os.replace`. A crash mid-write
  cannot leave a torn index.
- Reads: mtime check (`faiss_store.py:201`) reloads a partition another process
  changed.
- All FAISS work runs in `asyncio.to_thread` — the library is synchronous and
  would otherwise block the event loop.

---

## 4. Tenant isolation

Three overlapping mechanisms.

**Structural.** Every method on `MemoryRepository` takes a `MemoryScope`. There
is no method that reads a memory without one, so a caller cannot forget the
filter — there is nothing to forget.

**Physical.** FAISS partitions per scope. Cross-tenant retrieval isn't prevented,
it's *unrepresentable* — the vectors are in a different file.

**Defensive.** `memory_repository.py:44` (`_guard`) re-checks every document
against the requesting scope after it is read and raises `TenantIsolationError`
on mismatch. The query filter already guarantees this; the assertion costs a dict
comparison and a silent cross-tenant read is the worst failure this system could
have.

### Identity derivation

`app/core/identity.py`

```
userId          = "user_" + HMAC-SHA256(salt, "user"  | clientId | botId | E.164)[:24]
phoneNumberHash =          HMAC-SHA256(salt, "phone" | E.164)[:32]
```

Scoped **per (client, bot)** deliberately: the same person contacting two clients
gets two unrelated identifiers, so identity cannot be correlated across tenants
by comparing stored ids. The raw number is never persisted and the extraction
prompt forbids writing it into content.

`DREAM_IDENTITY_SALT` is therefore a long-lived secret — change it and every user
re-partitions.

---

## 5. Retrieval

`app/memory/retriever.py:121` (`search`)

```
1. Mongo   eligible_ids()   scope + status + type + validity → allow-list
2. FAISS   search(allowed_ids=…)                             → ranked hits
3. Mongo   get_many()                                        → canonical rows
4. Ranker  rank()                                            → hybrid score
```

**Filter first.** Taking the top-k vectors and filtering afterwards lets an
ineligible memory *displace* a valid one before you can look at it. Filtering
after can only shrink a bad list, never recover what was pushed out.

An empty allow-list short-circuits — and `faiss_store.py` treats
`allowed_ids=set()` as "nothing eligible", never as "no filter".

### The hybrid score

`app/memory/ranker.py:score`

```
score = ( 0.60·similarity + 0.15·importance + 0.10·confidence
        + 0.10·recency    + 0.05·status ) / Σweights
      × status_multiplier
```

The multiplier is separate from the additive status term and does the real work.
At weight 0.05 the active-vs-superseded gap is worth 0.03 of final score — easily
lost to a retired memory phrased closer to the query. A superseded fact isn't
marginally worse evidence, it's **false**, so it's multiplied by 0.45.

Recency decays from `updatedAt`, not `createdAt`: a memory re-confirmed last week
is current knowledge however old it is.

Every result carries `scoreBreakdown`, so a ranking is explainable rather than
inferred.

### Candidate selection for Dream

`retriever.py:184` (`find_related`) — different from search, and unions two
channels:

1. **Vector neighbours** above the supersede threshold.
2. **Recent active memories of the same type**, regardless of similarity.

Channel 2 exists because similarity is not a reliable proxy for "affected", and
*how* unreliable depends on the embedder. Under `text-embedding-3-small`,
"lives in Chennai" vs "lives in Bangalore" scores **0.77** — channel 1 finds it
easily. Under a lexical embedder the same pair scores **0.0**. Since the
framework is provider-independent, candidate selection must not silently depend
on the embedder being good. One indexed query buys that.

Patterns are excluded from both — a synthesised generalisation must never be
retired by an incoming raw fact.

---

## 6. The Dream engine

`app/dream/engine.py`

### Decision modes

`unified` (default) — one call, `DECISION_SYSTEM` (`prompts.py:153`), chooses
among all four actions.

`staged` — merge-focused call, then supersede-focused call, each with its own
prompt (`prompts.py:188`, `prompts.py:202`). ~2× tokens, better on hard cases,
easier to debug because each call answers one question. Merge runs first: it's
the cheaper mistake and the narrower question, and if the fact is simply a
restatement the second call is skipped.

Set via `dream.decision_mode`.

### Prompt composition

All prompt text lives in **one file**, `app/dream/prompts.py` (364 lines).

```
_SUPERSEDE_RULES  (prompts.py:91)   11 rules
_MERGE_RULES      (prompts.py:116)  10 rules
_SHARED_CLOSING   (prompts.py:139)  "you recommend, you do not execute"
        │
        ├──► DECISION_SYSTEM   (f-string composing both)
        ├──► SUPERSEDE_SYSTEM
        └──► MERGE_SYSTEM
```

Editing `_SUPERSEDE_RULES` updates the unified *and* focused prompts together, so
they cannot drift.

### What the model sees

`prompts.py:_memory_view` — tenancy fields are **deliberately absent**. The model
has no business knowing which client this is, and cannot reason about scope it
never sees.

---

## 7. Validation — the safety boundary

`app/dream/validation.py:47`

A `DreamDecision` is an untrusted suggestion. Before anything reaches the
database:

- **Target ids are intersected with the ids the model was shown.** A
  hallucinated id is *dropped, not looked up*. This is what makes it impossible
  for a model to reach another tenant's memory by guessing — the id never becomes
  a query.
- Scores clamped to `[0, 1]`.
- Tenancy taken from the request scope, never from model output.
- Disabled operations degrade rather than fail: with merge off, a `MERGE`
  proposal becomes `ADD`.
- Target-less `SUPERSEDE`/`MERGE` → `ADD`; content-less decision → `NOOP`.

Two protections are enforced in **code**, not prompt (`engine.py:490`):

```python
patterns          → refused: re-evaluated by synthesis, not retired by raw facts
permanent facts   → refused: where you were born doesn't change when you move
```

Every correction is recorded in `validationNotes` on the audit row, so an
overruled model is visible rather than silent.

---

## 8. Lifecycle transitions

`app/memory/lifecycle.py` — the **only** code allowed to change a memory's status.

```
NEW ──► ACTIVE ──┬──► SUPERSEDED
                 ├──► MERGED
                 └──► EXPIRED
```

Terminal states have no outgoing edges (`lifecycle.py:34`). History is immutable:
a superseded memory can never be re-superseded or revived. That rule is also what
makes replay safe — a decision naming an already-moved memory skips it and
converges instead of corrupting the chain.

Each transition keeps four things consistent:

```
canonical row → relationship edges → vector index → dependent pattern staleness
```

### Supersede (`lifecycle.py:96`)

New memory written **first**, then old ones retired. A crash between the two
leaves a duplicate active memory — recoverable, and visible to the next Dream
pass. The reverse order would leave the user with neither fact.

The old row keeps its content, gains `supersededBy`, and has `validUntil` closed
where the new fact's validity opens. It **stays indexed** — historical search
needs it.

### Merge (`lifecycle.py:153`)

The canonical memory inherits sources, provenance and the **earliest**
`validFrom` of everything it absorbs — a merge is a rewording, not a
rediscovery.

Merged rows **leave the index**, unlike superseded ones: their content now lives
in the canonical memory, so keeping them searchable would return the same fact
twice under two ids.

### Pattern staleness (`lifecycle.py:323`)

When a memory changes state, patterns resting on it are flagged `stale` — not
deleted. A pattern built on four memories doesn't become false because one was
reworded. The next synthesis run decides.

---

## 9. Synthesis

`app/dream/synthesis.py`. Background only.

```
POST /v1/dream/synthesize
  → job row in Mongo (status=queued)
  → job id into Redis
  → API returns immediately
                          ARQ worker picks it up
                          → run_synthesis_task (workers/synthesis_worker.py)
                          → SynthesisEngine.run  (synthesis.py:90)
```

### Pipeline

```
active memories (≤1000)
   │  candidate_selector.py:select      importance·0.35 + type·0.25
   │                                    + confidence·0.20 + recency·0.20
   ▼  ~200
   │  clustering.py:cluster_memories    agglomerative, average linkage
   ▼  ~20 clusters
   │  per cluster:
   │    source-overlap check ─── skip BEFORE any LLM call
   │    LLM (SYNTHESIS_SYSTEM)
   │    source validation ────── ids must come from this cluster
   │    quality bar ──────────── confidence/importance floors
   │    semantic duplicate ───── embedding comparison
   ▼
pattern memory + derived_from edges
```

**Clustering is agglomerative, not k-means** (`clustering.py`), because the
number of patterns is unknown — it's what we're discovering. Average linkage, not
single linkage, which would chain unrelated memories through weak links.
Implemented directly: a few hundred short vectors is an O(n² log n) loop
finishing in milliseconds, not worth a scientific-computing dependency.

### Idempotency

The load-bearing property. Synthesis runs nightly, so a duplicate-detection bug
doesn't produce one wrong pattern — it produces a new one every night.

Two independent checks:

| Check | Code | Catches |
|---|---|---|
| Source overlap | `synthesis.py:467` | Same evidence, any wording. Runs **before** the LLM call |
| Semantic similarity | `synthesis.py:490` | Same idea from *different* evidence |

Either firing → reinforce the existing pattern if the new evidence adds
something, else skip. **Never create.** Reinforcement keeps the pattern's id and
raises confidence with a 0.97 ceiling — inference must never claim the certainty
of testimony.

Within one run, patterns created for cluster *n* are visible to cluster *n+1*'s
duplicate check (`synthesis.py:130`), or a single run could emit two versions of
one idea.

### Traceability

`sourceMemoryIds` are validated against the cluster the model was shown
(`synthesis.py:257`). Fabricated ids are dropped; if too few survive the pattern
is rejected entirely. A generalisation nobody can audit back to evidence is an
unfalsifiable claim about a person.

Source memories are never modified. Synthesis is purely additive.

---

## 10. Manual management

The Dream layer decides what to keep from *conversation*. These are the operator
escape hatch, and they bypass the reasoner entirely — the caller has stated
exactly what they want, so there is nothing to infer.

| Endpoint | Code | Behaviour |
|---|---|---|
| `POST /v1/memories/manual` | `service.py:149` | Verbatim. No LLM, no policy |
| `PATCH /v1/memories/{id}` | `service.py:225` | In place, same id, active only |
| `DELETE /v1/memories/{id}` | `lifecycle.py:259` | → `expired`, row kept |
| `DELETE …?permanent=true` | `lifecycle.py:289` | Row + edges erased |
| `DELETE /v1/memories?confirm=ERASE` | `lifecycle.py:306` | Whole user, audit included |

### Design points

**Manual create still embeds and indexes**, and still checks `contentHash` — an
identical active memory is returned rather than duplicated, so an importer re-run
converges. `allowDuplicate: true` overrides. Rows are tagged
`metadata.source = "manual"`.

**Editing is in place, not a supersede.** An operator fixing a bad extraction is
correcting a *record*; a supersede chain would claim the user's situation
changed, putting a falsehood into the history. Content edits re-embed
(`service.py:264`) — an index entry disagreeing with its own text is a silent
retrieval bug.

**Terminal memories cannot be edited** (`409`). History is the audit trail; if it
can be rewritten it's worth nothing.

**Forget and Erase are separate verbs.** "Stop using this" and "destroy this"
carry very different risk. Forget is reversible and auditable; Erase requires an
explicit flag.

**Purge deletes the audit trail too** (`memory_repository.py:560`). Retaining
"we decided to store X about this user" after deleting X would defeat an erasure
request — which is also why purge writes no audit row of its own.

**Erasure clears dangling pointers** (`memory_repository.py:516`): inbound
`supersededBy`, `mergedInto` and `sourceMemoryIds` referencing a destroyed row
are cleared. A memory pointing at something that no longer exists is worse than
one pointing at nothing.

All manual operations write audit rows with `provider: "manual"` and
`model: null` — saying explicitly that no model was involved.

---

## 11. Failure behaviour

| Failure | Behaviour | Why |
|---|---|---|
| Extraction fails | Return "no memories", log | A message that can't be mined is a message with no memories — already the common case |
| Decision fails | Fall back to `ADD` (`engine.py:212`) | Storing unconsolidated is recoverable; discarding the user's information is not |
| Audit write fails | Log and swallow | The memory is already committed; failing the request would be a lie |
| One synthesis cluster fails | Skip it, continue | One bad cluster must not fail the run |
| LLM returns bad JSON | One repair round-trip with the validation error fed back (`azure_openai.py:225`) | Models correct reliably when shown the error |
| Model output truncated | Explicit error naming `max_tokens` | Otherwise surfaces as a confusing "unterminated object" |
| FAISS index corrupt | Log, start empty, keep serving | Mongo holds every embedding; `rebuild_index.py` restores |
| Mongo down | `/ready` returns 503 naming it | `/health` stays up so the orchestrator doesn't restart on a blip |

---

## 12. Provider abstraction

`app/memory` and `app/dream` import provider **base classes only** — never a
concrete implementation. That invariant is what keeps Azure OpenAI a first
implementation rather than a dependency.

| Interface | Methods | Shipped |
|---|---|---|
| `LLMProvider` | `generate_json` | Azure OpenAI, offline fake |
| `EmbeddingProvider` | `embed`, `embed_batch` | Azure OpenAI, offline fake |
| `VectorStore` | `upsert`, `search`, `delete`, `update`, `count`, `clear` | FAISS, in-memory |
| `JobQueue` | `enqueue_synthesis`, `health_check`, `close` | ARQ, inline |

Everything is constructed in one place — `app/container.py`, the composition
root. The API and the worker build the *same* container, so there's no second
wiring path to drift.

`LLMProvider.generate_json` returns a validated Pydantic model, never text. If
the model can't produce something satisfying the schema, that's a provider error,
not a parsing problem for the caller.

### The offline fakes are not stubs

`FakeEmbeddingProvider` is a feature-hashing bag-of-words model with signed
hashing and sublinear term scaling — related texts genuinely score higher than
unrelated ones.

`FakeLLMProvider` implements real rules: change-frame detection, single-valued
predicate conflicts, containment-based enrichment, source-overlap idempotency.
It scores **84.8%** on the 250-case evaluation set. That's what makes the test
suite meaningful without a network — a stub returning `ADD` for everything would
let every test pass while exercising nothing.

---

## 13. Where to change things

| To change… | Edit |
|---|---|
| How supersede/merge decide | `app/dream/prompts.py:91` / `:116` — highest leverage in the codebase |
| What gets extracted | `EXTRACTION_SYSTEM`, `prompts.py:28` |
| Thresholds | `config/dream.yaml` — measure first with `python -m scripts.calibrate` |
| Ranking weights | `retrieval.weights` + `status_multipliers` |
| What may be stored | `app/core/policy.py` |
| Backend protections | `engine.py:490` (`_protect_from_supersede`) |
| Add an LLM/vector/queue provider | Implement the base class, add one line to the factory |
| Rebuild the index | `python -m scripts.rebuild_index [--re-embed]` |

### Measured behaviour on `text-embedding-3-small`

From `python -m scripts.calibrate`, 27 labelled pairs:

| Relationship | Cosine |
|---|---|
| unrelated | 0.16 – 0.36 |
| duplicate | 0.46 – 0.87 |
| contradiction | 0.57 – 0.91 |
| near-identical | 0.94 – 1.00 |

Two counter-intuitive results worth internalising: **the noise floor is high**
(unrelated reaches 0.36, so a 0.25 threshold is *below the noise*), and
**contradictions score higher than duplicates** — they differ by one token within
a shared predicate, while a paraphrase can be lexically unrecognisable. Merge
therefore cannot take a stricter threshold than supersede; the bands overlap, and
separating them is the reasoner's job.

### Observed latency (Azure OpenAI, gpt-4o-mini + text-embedding-3-small)

```
embedding          ~480 ms
LLM call          ~1800 ms
full Dream decision ~2600 ms
retrieval          ~660 ms
synthesis (21 memories, 1 cluster)  ~7900 ms
exact duplicate      ~40 ms   ← no embedding, no LLM
```
