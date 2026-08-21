# Architecture

This document covers how Dream Memory is put together and, more usefully, why
each load-bearing decision went the way it did.

---

## 1. The shape of the problem

A vector store plus an `add()` call is a memory system right up until the user
contradicts themselves. Then you have two active memories saying different
things, retrieval returns whichever is phrased closer to the query, and the
agent answers with whichever it got.

So the interesting operations are not storage and retrieval. They are:

- deciding that a new fact **replaces** an old one, and retiring the old one
  without losing it;
- deciding that a new fact is the **same** fact said better, and consolidating;
- noticing, across many memories, a **pattern** none of them states;
- doing all of this without ever letting one tenant's data reach another.

Everything below follows from those four.

---

## 2. Two stores, one source of truth

```
        ┌──────────────────────────────┐
        │           MongoDB            │   source of truth
        │  memories · relations        │   every field, every embedding
        │  dream_operations · jobs     │
        └──────────────┬───────────────┘
                       │  derived, disposable
                       ▼
        ┌──────────────────────────────┐
        │            FAISS             │   semantic index only
        │  one partition per tenant    │
        └──────────────────────────────┘
```

Mongo holds the canonical record, including the embedding. FAISS holds a copy of
the vector and nothing else.

This split is what makes the index disposable. Corrupt it, delete it, change
embedding model — `scripts/rebuild_index.py` regenerates it from Mongo. If the
vector store were the source of truth, an index problem would be a data-loss
event.

It also means metadata filtering happens in a database that is good at it. The
retrieval path is:

```
Mongo: scope + status + type + validity window  →  set of eligible ids
FAISS: rank *only those ids* by similarity
Ranker: blend similarity with importance, confidence, recency, status
```

Filter first, then rank. The other order — take the top-k vector hits and filter
afterwards — lets a superseded or out-of-window memory push a valid one out of
the result set before you ever get to look at it.

### Why MongoDB

Memory documents are irregular by nature: an open `metadata` bag, a variable
`sourceMemoryIds` list, an optional embedding array of a model-dependent width.
A document store fits that without migrations every time a memory type acquires
a field.

The cost is no multi-document transaction by default, and supersede touches
several documents. Section 6 covers how ordering is chosen so that a crash
mid-operation is recoverable rather than lossy.

### Why FAISS, and how tenancy works

FAISS has no metadata filter. A single shared index would mean fetching
neighbours and *then* discarding other tenants' rows — a design where one
filtering bug is a data leak.

So the index is partitioned physically: one FAISS index file per
`(clientId, botId, userId)`, named by a SHA-256 of the triple. Cross-tenant
retrieval is not something the code avoids; it is something the code cannot
express, because the vectors are in a different file.

Partitioning has a second benefit. Each index holds one user's memories —
hundreds to low thousands — which is small enough that `IndexFlatIP` over
L2-normalised vectors is exact cosine search with no approximation and no
training step.

Two processes write (the API and the worker). Correctness comes from three
things: an inter-process `FileLock` around every read-modify-write, an mtime
check that reloads a partition another process has changed, and a temp-file +
`os.replace` write so a crash cannot leave a torn index. All FAISS work runs in
a thread, since the library is synchronous and would otherwise block the loop.

This is correct for a few processes on shared storage. It is not a horizontal
scale-out design — see §10.

---

## 3. Tenancy

Three mechanisms, deliberately overlapping.

**Structural.** Every repository method takes a `MemoryScope`. There is no
method that reads or writes a memory without one, so a caller cannot forget the
filter — there is nothing to forget.

**Physical.** FAISS partitions per scope, as above.

**Defensive.** After a document is read, `MemoryRepository._guard` re-checks it
against the requesting scope and raises `TenantIsolationError` if it does not
match. The query filter already guarantees this; the check exists because a
silent cross-tenant read is the worst failure this system could have, and the
assertion costs a dict comparison.

### Identity

The API accepts a phone number, because that is what messaging platforms have.
It is immediately turned into:

```
userId          = "user_" + HMAC-SHA256(salt, "user" | clientId | botId | E.164)[:24]
phoneNumberHash =          HMAC-SHA256(salt, "phone" | E.164)[:32]
```

and the raw number is dropped. It is never written into memory content, and the
extraction prompt forbids it explicitly.

`userId` is salted **per (client, bot)**. The same person contacting two clients
gets two unrelated identifiers, so identity cannot be correlated across tenants
by comparing stored ids — which would otherwise be a real leak in a
multi-tenant deployment where several clients serve overlapping user bases.

The salt is therefore a long-lived secret: change it and every user
re-partitions.

---

## 4. The real-time path

```
prompt
  │
  ├─ extraction (LLM) ──────────── one message may hold several facts
  │
  ├─ memory policy ─────────────── type allow/block, confidence and importance
  │                                floors, sensitivity, per-prompt cap
  │
  └─ for each candidate, sequentially:
        │
        ├─ exact-duplicate check ── indexed contentHash lookup
        │                           short-circuits to NOOP, no LLM, no embedding
        ├─ embed
        ├─ candidate retrieval ──── §5
        ├─ decision (LLM) ────────── ADD | SUPERSEDE | MERGE | NOOP
        ├─ validation ────────────── §6
        ├─ state transition ──────── §7
        └─ audit
```

Candidates are processed **sequentially, not concurrently**. Each is reasoned
about against the state the previous one left behind. A message that states the
same thing twice must not produce two active memories, which is exactly what a
parallel pass would create.

### What is deterministic

The LLM is used for extraction, the ADD/SUPERSEDE/MERGE/NOOP judgement,
canonical text generation, and synthesis. Everything else is code: filtering,
authorisation, tenant isolation, vector search, ranking, validation, state
transitions, idempotency, job management.

The exact-duplicate check earns its place here. In chat-shaped traffic users
repeat themselves constantly, and a normalised-hash lookup catches it for the
cost of an indexed query.

---

## 5. Candidate selection, and why similarity is not enough

The reasoner sees the memories most likely to be affected. Getting that set
wrong is invisible: if the contradicted memory is not in the set, the reasoner
returns `ADD`, and nothing looks broken.

Candidate selection unions two channels:

1. **Vector neighbours** above `dream.supersede.similarity_threshold`.
2. **The most recent active memories of the same type**, regardless of
   similarity — one indexed Mongo query.

Channel 2 exists because similarity alone is not a reliable proxy for
"affected". How unreliable depends entirely on the embedder:

- Under a strong model like `text-embedding-3-small`, contradictions actually
  score *high* — "lives in Chennai" and "lives in Bangalore" share their
  predicate and score 0.77. Channel 1 finds them easily, and channel 2 is
  cheap insurance for cases where phrasing diverges further ("relocated", "am
  now based in") or where a user's memories are unusually varied.
- Under a weaker or purely lexical embedder — including the offline fake used
  in tests — the same pair scores **0.0**, because it shares no content words.
  There, channel 2 is the only thing that makes supersede work at all.

Since the framework is provider-independent by design, candidate selection must
not silently depend on the embedder being good. One indexed query buys that.

Patterns are excluded from both. A synthesised generalisation must not be
retired or merged by an incoming raw fact; it is re-evaluated by synthesis.

### Threshold calibration

Similarity thresholds are the most common misconfiguration, because cosine
similarity reads like a percentage and is not one. Measured for
`text-embedding-3-small` on short third-person facts, via
`python -m scripts.calibrate`:

| relationship | measured cosine |
|---|---|
| unrelated | 0.16 – 0.36 |
| duplicate | 0.46 – 0.87 |
| contradiction | 0.57 – 0.91 |
| near-identical | 0.94 – 1.00 |

Two results are worth stating plainly, because both contradict the intuition
that produced the first draft of these defaults:

**The noise floor is high.** Unrelated text reaches 0.36. A threshold of 0.25
looks conservative and is in fact *below the noise*.

**Contradictions score higher than duplicates, and the bands overlap.** A
contradiction usually differs by a single token within a shared predicate
("lives in Chennai" / "lives in Bangalore" → 0.77), whereas a paraphrase can be
lexically unrecognisable ("likes black coffee" / "prefers coffee without milk" →
0.46). So merge cannot be given a stricter threshold than supersede on the
theory that it is "a closer relationship" — it isn't, measurably. Both sit at
0.40, and distinguishing contradiction from duplication is left to the reasoner,
which is the only component that can actually tell them apart.

Shipped defaults: 0.30 retrieval floor, 0.40 supersede, 0.40 merge, 0.45
clustering. Re-run the calibration script if you change embedding model; the
bands move substantially.

---

## 6. Validating what the model returned

A `DreamDecision` is an untrusted suggestion. `app/dream/validation.py` reduces
it to something safe before anything reaches the database:

- **Target ids are intersected with the ids the model was shown.** A
  hallucinated id is dropped, not looked up. This is what makes it impossible
  for a model to reach another tenant's memory by guessing — the id never
  becomes a query.
- **Scores are clamped** to `[0, 1]`.
- **Tenancy comes from the request's `MemoryScope`**, never from model output.
- **Disabled operations degrade rather than fail.** With merge switched off, a
  `MERGE` proposal becomes `ADD`: the fact is still worth keeping, it just is
  not consolidated.
- **A target-less `SUPERSEDE` or `MERGE` becomes `ADD`**; a content-less
  decision becomes `NOOP`.

Two protections are enforced in code rather than left to the prompt, because a
single bad decision would destroy the current answer to a question:

- **Patterns cannot be superseded** by a raw fact.
- **Permanent facts cannot be superseded.** Where someone was born does not stop
  being true when they move.

Every correction is recorded in `validationNotes` on the audit row, so an
overruled model is visible rather than silent.

---

## 7. Lifecycle

```
NEW ──► ACTIVE ──┬──► SUPERSEDED
                 ├──► MERGED
                 └──► EXPIRED
```

Terminal states have no outgoing edges. History is immutable: a superseded
memory can never be re-superseded or revived.

That rule is also what makes replay safe. A Dream operation naming a memory that
has already moved on skips it silently and converges, instead of rewriting a
pointer and corrupting the chain.

`MemoryLifecycle` is the only code that changes status, because each transition
must keep four things consistent:

```
canonical row → relationship edges → vector index → dependent pattern staleness
```

### Supersede

The new memory is written **first**, then the old ones are retired. If the
process dies between the two, the outcome is a duplicate active memory —
recoverable, and visible to the next Dream pass. The reverse order would leave
the user with neither the old fact nor the new one.

The old memory keeps its content in full, gains `supersededBy`, and has its
`validUntil` closed at the moment the new fact's validity begins. It **stays in
the vector index**: "where did they live before?" is a question this system
exists to answer.

### Merge

The canonical memory inherits the sources, provenance and earliest `validFrom`
of everything it absorbs — a merge is a rewording, not a rediscovery, and
retrieval recency should reflect that.

Merged memories **leave the index**, unlike superseded ones. Their content is
now fully contained in the canonical memory, so leaving them searchable would
return the same fact twice under two ids.

### Pattern staleness

When a memory changes state, patterns resting on it are flagged `stale` — not
deleted. A pattern built on four memories does not become false because one was
reworded, and silently deleting derived knowledge on every edit would make the
layer unstable. The next synthesis run decides.

---

## 8. Synthesis

Runs in the background. Never on a request path — it is measured in seconds.

```
active memories (up to ~1000)
  │
  ├─ candidate selection ─────── importance, type weight, confidence, recency
  │                              deterministic; → ~200
  ├─ agglomerative clustering ── average linkage over cosine; → ~20 clusters
  │
  └─ per cluster:
        ├─ source-overlap check ── skip before any LLM call
        ├─ synthesis (LLM)
        ├─ source validation ───── ids must come from this cluster
        ├─ quality bar ─────────── confidence and importance floors
        ├─ semantic duplicate ──── embedding comparison to existing patterns
        └─ create, or reinforce an existing pattern in place
```

**Clustering** is agglomerative rather than k-means because the number of
patterns in a user's memory is unknown — it is what we are trying to discover —
and a similarity cut-off expresses "these belong together" directly, whereas a
value of `k` does not. Average linkage, not single linkage, which would chain
unrelated memories together through a sequence of weak links. Implemented
directly: a few hundred short vectors is an O(n² log n) loop that finishes in
milliseconds, and a scientific-computing dependency in a memory service is not
worth the install size.

**Idempotency** gets two independent checks, because the failure mode compounds.
Synthesis runs nightly; without them a user accumulates fifty near-identical
"prefers morning meetings" patterns that poison every subsequent retrieval.

1. *Source overlap* — deterministic, and runs **before** the LLM call. If a
   pattern already rests on substantially the same evidence, this cluster has
   been synthesised before, whatever words the model would choose this time.
   Overlap is measured against the smaller source set, so a broad pattern
   absorbing a narrow one still counts as covering it.
2. *Semantic similarity* — embedding comparison against existing patterns.
   Catches the same idea derived from a *different* subset of evidence, which
   the first check cannot see.

Either firing means: reinforce the existing pattern if the new evidence adds
something, otherwise skip. Never create. Reinforcement keeps the pattern's id,
so anything referencing it stays valid, and raises confidence with a ceiling of
0.97 — an inferred pattern must never claim the certainty of testimony.

Within a single run, patterns created for cluster *n* are visible to the
duplicate check for cluster *n+1*. Otherwise one run could emit two versions of
the same idea.

**Traceability is mandatory.** `sourceMemoryIds` are validated against the
cluster the model was shown; fabricated ids are dropped, and if too few survive,
the pattern is rejected. A pattern nobody can audit back to evidence is worse
than no pattern.

Source memories are never modified. Synthesis is purely additive.

---

## 9. Ranking

```
score = ( w_sem·similarity + w_imp·importance + w_conf·confidence
        + w_rec·recency    + w_status·status_score ) / Σw
      × status_multiplier
```

All weights are configuration, and every component travels with the result in
`scoreBreakdown`, so a ranking can be explained rather than guessed at.

The multiplicative term deserves its own note, because it is an addition to the
specified formula rather than a reading of it. At the specified status weight of
0.05, the difference between active and superseded is worth 0.03 of final
score — easily outweighed by a retired memory that happens to phrase things
closer to the query. That produces exactly the failure this system exists to
prevent: asking where the user lives and getting the city they left.

A superseded fact is not marginally worse evidence, it is *false*. The
multiplier (0.45 by default) demotes it accordingly. Set every entry to 1.0 to
recover the pure additive formula.

Recency decays from `updatedAt` rather than `createdAt`: a memory re-confirmed
last week is current knowledge even if first learned years ago.

---

## 10. Operational notes

**Scaling.** The API is stateless apart from the FAISS files. To run more than a
few replicas, implement `VectorStore` against a vector *server* (Qdrant,
pgvector, Azure AI Search) — the interface is four methods and every caller
already passes a scope. Nothing else changes.

**The queue.** No broker, and not because one was hard to add — because the
workload does not justify one. Synthesis is idempotent and its trigger is a
durable fact rather than an event, so a lost job re-fires on the next write.
That removes the only thing a broker guarantees. By default it runs in a bounded
pool of tasks inside the API; `DREAM_QUEUE_PROVIDER=database` moves it to a
worker process claiming rows from the jobs table, for deployments running
several API replicas. A worker runs the
same coroutines the API does, with no `asyncio.run()` bridge inside a sync task
and no second code path to keep correct. `JobQueue` is four methods if you
prefer Celery, SQS, Service Bus or Temporal.

**Job state lives in Mongo, not the queue.** The queue carries only a job id.
Status is answerable whatever broker is installed, and a flushed queue loses
scheduling but never the record that work was requested.

**Failure modes.** A reasoning failure falls back to `ADD` — storing a fact
unconsolidated is recoverable, discarding it is not. An audit write failure is
logged and swallowed, because the memory is already committed by then and
failing the caller's request would be a lie. One cluster failing does not fail a
synthesis run.

**Observability.** Structured logs carry `request_id`, `operation_id` and the
scope triple through context variables, so a decision traces end to end without
threading a logger through the call stack. Counters and latency histograms are
at `GET /v1/metrics`, and mirror to OpenTelemetry when `DREAM_OTEL_ENABLED=true`.

---

## 11. Module map

```
app/
  main.py              FastAPI app, middleware, error translation
  container.py         composition root — the only place providers are constructed

  api/                 routes; deps.py owns auth and scope resolution
  core/                config, logging, metrics, exceptions, identity, policy
  models/              Memory, MemoryScope, DreamDecision, DreamJob, requests
  memory/              extractor, retriever, ranker, lifecycle, service
  dream/               engine, supersede, merge, synthesis, clustering,
                       candidate_selector, validation, prompts
  providers/           llm, embeddings, vector_store, queue — each with a base
                       class, a concrete implementation, and an
                       offline implementation
  repositories/        Mongo access; every method takes a MemoryScope
  workers/             optional worker for DREAM_QUEUE_PROVIDER=database
```

`app/memory` and `app/dream` import provider **base classes** only. That is the
invariant that keeps Azure OpenAI a first implementation rather than a
dependency, and it is worth preserving.
