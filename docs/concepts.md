# Concepts

MemDream is long-term memory for AI agents. Your agent hands it what a user says;
MemDream decides what is worth remembering, keeps it consistent as the user's
situation changes, and returns the right memories when the agent needs them.

This page explains the model behind it. The two ways to use MemDream share every
concept here:

| | [Python SDK](sdk.md) | [REST API](api.md) |
|---|---|---|
| **What it is** | `MemDream` runs the engine inside your process, or `DreamClient` calls a server | a standalone HTTP service |
| **Use it when** | your agent is written in Python | any language, several services sharing one memory, or central control |
| **Deploy** | nothing (embedded) | the MemDream service and one database — see [Self-hosting](self-hosting.md) |

The SDK's embedded and hosted forms have the same methods, arguments, return
types and exceptions, so moving from one to the other changes one line.

---

## Contents

1. [The three verbs](#the-three-verbs)
2. [Scope: whose memory is it?](#scope-whose-memory-is-it)
3. [The memory object](#the-memory-object)
4. [Nothing is deleted: the lifecycle](#nothing-is-deleted-the-lifecycle)
5. [What happens when you remember](#what-happens-when-you-remember)
6. [How recall ranks memories](#how-recall-ranks-memories)
7. [Dream: synthesis and consolidation](#dream-synthesis-and-consolidation)
8. [Describing your domain](#describing-your-domain)

---

## The three verbs

**Remember.** Send a message. MemDream extracts the durable facts in it and, for
each one, decides whether it is new (`ADD`), replaces something that is no longer
true (`SUPERSEDE`), restates something already known (`MERGE`), or is not worth
keeping (`NOOP`).

**Recall.** Ask a question. MemDream returns the memories that answer it, ranked
by meaning *and* by how important, reliable, recent and current each one is.

**Dream.** In the background, MemDream finds patterns across many memories
(*synthesis*), and once a night retires memories that have stopped being true or
useful (*consolidation*).

```text
"Hi, I'm Priya. I live in Chennai."        → ADD        User lives in Chennai.
"I'm allergic to peanuts."                 → ADD        User is allergic to peanuts.
"hey thanks!"                              → NOOP       (no model call, no cost)
"I live in Bangalore now."                 → SUPERSEDE  User lives in Bangalore.
                                                        └ Chennai kept as history

recall "where does the user live?"         → User lives in Bangalore.
```

---

## Scope: whose memory is it?

Every memory belongs to exactly one **scope**, and every operation runs inside
one. A scope is made of up to four dimensions:

| Dimension | Required | Meaning |
|---|---|---|
| `userId` *or* `phoneNumber` | **yes** | the person. The isolation boundary |
| `agentId` | no | which agent or bot, when you run several |
| `clientId` | no | the tenant, when you serve several customers |
| `namespace` | no | anything else: `{"env": "prod", "region": "in"}` |

A single-agent app sends `userId` and nothing else.

Each distinct combination is a separate **partition**. A memory written with
`{userId: "u1", agentId: "support"}` is invisible to a request for
`{userId: "u1"}` alone, and to `{userId: "u1", agentId: "sales"}`. Two
consequences:

- **Decide your dimensions before you have data.** Adding `agentId` later does
  not widen the existing partition; it starts a new, empty one.
- **Send the same dimensions on every call.** Omitting one reads a different
  partition, which looks exactly like the memory not existing.

### Identity from a phone number

Messaging integrations often have a phone number rather than a user id. Send
`phoneNumber` and MemDream derives a stable `userId` from it — a salted hash
combined with `clientId` and `agentId`, so the same number under two tenants
never collides. The number itself is not stored on memories; only a salted hash
is. If you send both `userId` and `phoneNumber`, `userId` wins.

The salt is `DREAM_IDENTITY_SALT`. Changing it changes every derived id, so treat
it as permanent.

### Tenancy is enforced, not trusted

Reads by id are scoped too: a memory id is a random handle, not a permission.
Asking for another partition's memory returns **404**, never 403 — it is
indistinguishable from not existing.

On a multi-tenant deployment, API keys can be **bound** to a client. The tenant
then comes from the key, and a request naming a different `clientId` is refused.
See [Authentication](api.md#authentication).

---

## The memory object

```json
{
  "memoryId": "mem_002e9cd0a9d94445bb4edad2",
  "userId": "user_123",
  "agentId": null,
  "clientId": null,
  "namespace": {},
  "scopeKey": "9dc7ad5b46771f7e11d973ecee453a23",
  "phoneNumberHash": null,
  "content": "User lives in Chennai.",
  "memoryType": "fact",
  "status": "active",
  "importance": 0.8,
  "confidence": 0.9,
  "explicit": true,
  "temporalNature": "current",
  "createdAt": "2026-09-13T19:14:19.049000Z",
  "updatedAt": "2026-09-13T19:14:19.049000Z",
  "validFrom": "2026-09-13T00:00:00Z",
  "validUntil": null,
  "supersededBy": null,
  "mergedInto": null,
  "sourceMemoryIds": [],
  "sourceMessageIds": [],
  "embeddingModel": "azure_openai:text-embedding-3-small",
  "embeddingDim": 1536,
  "contentHash": "1c8a43ae4a818788ca51ca1b9663f437",
  "lastRecalledAt": null,
  "recallCount": 0,
  "stale": false,
  "lastSynthesizedAt": null,
  "metadata": {}
}
```

| Field | Meaning |
|---|---|
| `memoryId` | `mem_…`, or `pat_…` for a pattern produced by synthesis |
| `userId` `agentId` `clientId` `namespace` | the scope it belongs to |
| `scopeKey` | a digest of the scope, used internally as the partition filter |
| `phoneNumberHash` | salted hash, when the scope came from a phone number |
| `content` | the memory itself: one self-contained, third-person sentence |
| `memoryType` | open vocabulary. Well-known values: `profile`, `preference`, `goal`, `fact`, `event`, `relationship`, `constraint`, `skill`, `project`, `behavior`, `pattern`, `other`. Your own types are fine |
| `status` | `active`, `superseded`, `merged` or `expired` — see [lifecycle](#nothing-is-deleted-the-lifecycle) |
| `importance` | 0–1. How much this matters. Protects a memory from being forgotten |
| `confidence` | 0–1. How sure MemDream is that it is correct |
| `explicit` | `true` when the user said it; `false` when it was inferred. Patterns and summaries are always `false` |
| `temporalNature` | `permanent` (cannot change: a birthplace), `current` (true now: an address), `temporary`, `historical`, `future`. `permanent` memories are never superseded |
| `createdAt` `updatedAt` | when MemDream learned it, and last changed it |
| `validFrom` `validUntil` | when the *fact* is true — distinct from when it was learned. A memory whose `validUntil` has passed is retired by consolidation |
| `supersededBy` `mergedInto` | the memory that replaced or absorbed this one |
| `sourceMemoryIds` | what this was built from. Always set on patterns and summaries |
| `sourceMessageIds` | your message ids, from `messageId` on the request |
| `embeddingModel` `embeddingDim` | which model produced the vector. The vector itself is never returned |
| `contentHash` | normalised-content hash, used to catch exact duplicates cheaply |
| `lastRecalledAt` `recallCount` | how often search has actually returned it. Reading never changes `updatedAt` |
| `stale` | a pattern whose evidence has changed, flagged for re-evaluation |
| `lastSynthesizedAt` | when synthesis last considered it |
| `metadata` | yours. Stored and returned untouched, except that manual edits add `source` and `lastManualEdit` |

---

## Nothing is deleted: the lifecycle

MemDream never overwrites or deletes a memory as part of normal operation. It
moves memories between states, and keeps a pointer to whatever replaced them.

```text
                ┌──────────────► superseded   a newer fact made it untrue
                │                             (supersededBy → the new memory)
   active ──────┼──────────────► merged       folded into a memory that says
                │                             the same thing better
                │                             (mergedInto → that memory)
                └──────────────► expired      retired: forgotten on request,
                                              its validity window closed, or
                                              consolidated away
```

| Status | Returned by search | Why it still exists |
|---|---|---|
| `active` | yes | it is true now |
| `superseded` | yes by default, ranked lower | "where did they live before?" still has an answer |
| `merged` | only with `includeMerged` | its content already lives in the memory that absorbed it |
| `expired` | only with `includeExpired` | auditability; a wrong retirement can be traced |

`superseded` and `merged` are history and are never edited again. The only ways to
actually remove data are explicit: deleting one memory with `permanent=true`, or
erasing a user entirely. Both exist for data that must not exist — a wrongly
captured secret, or an erasure request.

Because history is kept, you can ask what was true at a point in time:
`asOf: "2026-03-01T00:00:00Z"` returns only memories whose validity window
contains that instant.

---

## What happens when you remember

```text
message
  │
  ├─ 1. triage ........ greetings, thanks, "ok" and other small talk stop here.
  │                     No model call, no cost. Recorded as NOOP.
  │
  ├─ 2. extract ....... a language model pulls out durable facts, each as one
  │                     third-person sentence with a type, importance,
  │                     confidence and temporal nature.
  │
  ├─ 3. candidates .... for each fact, find the existing memories it could relate
  │                     to: close in meaning, or recent and of the same type.
  │                     No model call.
  │
  ├─ 4. decide ........ the model proposes ADD, SUPERSEDE, MERGE or NOOP against
  │                     those candidates.
  │
  └─ 5. execute ....... MemDream validates the proposal and applies it. The model
                        proposes; the backend decides. It can only touch the
                        memories it was actually shown.
```

| Action | What happened |
|---|---|
| `ADD` | a new memory was stored |
| `SUPERSEDE` | a new memory was stored and replaced one or more that are no longer true |
| `MERGE` | the fact was already known; existing memories were consolidated |
| `NOOP` | nothing was stored. The reason is recorded in the audit log |

One message can contain several facts, so the response carries a decision per
fact. The top-level `action` mirrors the most significant one.

**Every decision is audited**, including every `NOOP` — "why wasn't this
remembered?" is answered by the audit log rather than by reading server logs.

**Cost.** Small talk costs nothing. A message with facts in it costs roughly one
extraction call and one decision call per fact that has related memories.
Measured on `gpt-4o-mini`: about 450 input tokens to extract, and about 600–950
to decide depending on how many candidates are shown. Token use is recorded per
operation and reported by [stats](api.md#get-v1dreamstats).

---

## How recall ranks memories

Similarity alone is the wrong ranking for memory: a vaguely related trivia
memory can out-score a critical allergy. MemDream blends five signals:

```text
score = 0.60 × semantic similarity
      + 0.15 × importance
      + 0.10 × confidence
      + 0.10 × recency
      + 0.05 × status          (current memories above superseded history)
```

Every search result includes its `score` and a `scoreBreakdown`, so a ranking
can always be explained. A real result for *"where does the user live?"*:

```json
{
  "content": "User lives in Chennai.",
  "score": 0.703343,
  "scoreBreakdown": {
    "semanticSimilarity": 0.572239,
    "importance": 0.8,
    "confidence": 0.9,
    "recency": 1.0,
    "status": 1.0
  }
}
```

0.60 × 0.572239 + 0.15 × 0.8 + 0.10 × 0.9 + 0.10 × 1.0 + 0.05 × 1.0 = **0.703343**.

Memories must clear a minimum similarity before they are ranked at all, so an
unrelated question returns nothing rather than your least-bad match. Recency
decays with a 90-day half-life. The weights and thresholds are configurable.

Tips:

- **`latestOnly: true`** when you want only what is true now — usually right when
  building a prompt.
- **`minScore`** to drop weak matches. In the example above, *"User is allergic
  to peanuts."* also came back for the address question, at `0.54`, because its
  importance is high.
- **`memoryTypes`** to restrict to, say, `["preference", "constraint"]`.

Each search also records which memories it returned (`lastRecalledAt`,
`recallCount`). That is the evidence consolidation uses to tell what is still
useful from what nothing has needed in months.

---

## Dream: synthesis and consolidation

### Synthesis — finding patterns

Individual memories say what happened. Synthesis looks across many of them and
records what they add up to:

```text
User ran before work on Monday.     ┐
User ran before work on Tuesday.    ├──►  pat_…  User exercises early on most
User ran before work on Wednesday.  │          weekday mornings.
User ran before work on Thursday.   ┘          sourceMemoryIds: [4 memories]
```

A pattern is a memory with `memoryType: "pattern"`, `explicit: false`, and a
non-empty `sourceMemoryIds` — every pattern cites the evidence it rests on, and
one without evidence is refused. If that evidence later changes, the pattern is
marked `stale` for re-evaluation, never silently deleted.

Synthesis runs in the background. It triggers automatically once a user has
accumulated 20 new memories (at most once every 6 hours per user), and you can
request it any time. It needs at least 20 active memories by default, because a
"pattern" drawn from two or three is coincidence, not insight.

### Consolidation — forgetting safely

Memory that only grows gets worse: old facts crowd out current ones and every
decision pays to look at them. Consolidation retires what no longer earns its
place. It always retires by moving memories to `expired` or `merged`, so nothing
is lost and every retirement is traceable.

It runs **every night** for every user (midnight UTC by default), before each
synthesis run, and on demand. It has three rules:

| Rule | Retires | Default | Costs |
|---|---|---|---|
| **lapsed** | memories whose `validUntil` has passed — provably no longer true | **on** | nothing |
| **decay** | memories nothing has recalled for a set number of days | off | nothing |
| **compaction** | crowds of near-identical, low-value memories, folded into one summary | off | one model call per group |

Decay and compaction are judgements rather than facts, so they are off until you
turn them on, and both have hard guards. They never touch:

- anything with importance above a floor — an allergy nobody has asked about in
  a year still outranks yesterday's lunch order;
- `permanent` facts;
- the evidence an active pattern rests on;
- anything recent.

Compaction additionally refuses any summary shorter than the longest memory it
would replace, because a summary that short has almost certainly dropped a fact.

**Measure before you enable.** A consolidation run is a *dry run* by default: it
reports what it would retire and changes nothing. Run it against real users with
different windows, and turn a rule on once the list stops surprising you.
See [`POST /v1/dream/consolidate`](api.md#post-v1dreamconsolidate) or
[`consolidate()`](sdk.md#consolidate).

On a year-old test account of 65 memories, a full pass left 19 active — every
allergy, address and name intact, and every folded memory still readable with a
pointer to its summary.

---

## Describing your domain

`DREAM_DOMAIN` is one sentence telling the extraction and decision steps what
your deployment is:

```bash
DREAM_DOMAIN="A restaurant ordering assistant. Remember contact and delivery details, dietary needs, dislikes, and usual orders per meal."
```

It costs about 30 tokens per call and is the cheapest accuracy improvement
available: it tells the model that *"no onions"* is a standing constraint rather
than small talk.

It also **narrows what gets extracted**, and a sentence that is too narrow will
skip facts that only *imply* what you care about. A delivery-focused domain can,
for example, pass over *"I moved to another city last month"* while correctly
handling *"I live in Bangalore now."* Test your domain sentence against
representative messages from real users, check the `NOOP` reasons in the audit
log, and leave it empty for a general-purpose assistant.
