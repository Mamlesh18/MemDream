# Writing your prompts

MemDream ships **no prompts**. The engine knows how to extract facts, decide
between adding and superseding, find patterns and compact old memories. What to
*say* to the model is yours, because it is the part that depends on your
product: a memory layer for a restaurant, a bank and a support desk should
remember different things, and a built-in prompt would be an invisible default
everyone inherits and nobody reviews.

So a deployment supplies prompts, and one that has not does not start:

```
No prompt configured for: extraction, decision.

MemDream ships no prompts; each deployment writes its own, because what is
worth remembering depends on your product.

  extraction — pull durable facts out of a message
  decision — choose ADD / SUPERSEDE / MERGE / NOOP for one fact (unified mode)

Supply them in any of these ways:
  1. python -m memdream prompts ./prompts   (copy the starter set, then edit)
     and set DREAM_PROMPTS_DIR=./prompts
  ...
```

---

## Contents

- [Get a working set](#get-a-working-set)
- [Where prompts come from](#where-prompts-come-from)
- [Which prompts you need](#which-prompts-you-need)
- [What each prompt receives and must return](#what-each-prompt-receives-and-must-return)
  - [extraction](#extraction) · [decision](#decision) · [supersede](#supersede) · [merge](#merge) · [synthesis](#synthesis) · [compaction](#compaction)
- [Writing a good prompt](#writing-a-good-prompt)
- [Testing a change](#testing-a-change)

---

## Get a working set

```bash
python -m memdream prompts ./prompts
```

That writes six files you can edit. They are templates, not defaults — nothing
loads them until you say so:

```bash
DREAM_PROMPTS_DIR=./prompts
```

```python
mem = MemDream(user_id="alice", prompts="./prompts")
```

`python -m memdream prompts --list` shows what each one decides.

---

## Where prompts come from

Two sources, highest precedence first:

| Source | How | Use it for |
|---|---|---|
| Inline text | `DREAM_PROMPTS__EXTRACTION="..."`, `prompts={"extraction": "..."}` in the SDK, or a `prompts:` block in `config/dream.yaml` | overriding one prompt, or holding prompts in your own config system |
| A directory | `DREAM_PROMPTS_DIR=./prompts` with `extraction.md`, `decision.md`, … | the normal case: prompts live in your repository and get reviewed like code |

Files may be `.md`, `.txt` or `.prompt`. **The whole file is the prompt** — there
is no front matter and no comment syntax, because anything in the file is sent
to the model.

Mixing is fine: keep five in files and override one with an environment variable
for an experiment.

### The one substitution

`{domain}` anywhere in a prompt is replaced with your `DREAM_DOMAIN` sentence as
a short context line, or with nothing when it is unset:

```bash
DREAM_DOMAIN="A restaurant ordering assistant. Remember contact and delivery details, dietary needs, dislikes, and usual orders per meal."
```

Nothing else is substituted. Braces in JSON examples are safe.

---

## Which prompts you need

Only the ones your configuration can reach, so the smallest working setup is two
files:

| Prompt | Needed when |
|---|---|
| `extraction` | always |
| `decision` | `decision_mode: unified` (the default) |
| `supersede`, `merge` | `decision_mode: staged` |
| `synthesis` | synthesis is enabled (default on) |
| `compaction` | compaction is enabled (default off) |

Missing prompts are reported at startup, all at once, by name.

`GET /ready` lists the prompts in force and where each was loaded from — the
first thing to check when one deployment behaves unlike another.

---

## What each prompt receives and must return

Every call has the same shape: **your prompt is the system message; the user
message is a single JSON object of data.** That separation is deliberate — a
memory whose text reads *"ignore previous instructions"* arrives as data, never
as instructions.

**You do not need to describe the JSON to return.** MemDream appends the exact
output shape to every prompt, and validates the reply against it. The shapes
below are what gets appended, shown so you know which field names your wording
should use.

### extraction

Pulls durable facts out of one message. The most important prompt you will
write: it decides what your product remembers at all.

**Receives:** the message, the time it was sent, and your message id.

**Must return:**

```
{"hasMemory":bool,"reason":str,"memories":[{
 "content":str,
 "memoryType":"profile|preference|goal|fact|event|relationship|constraint|skill|project|behavior|other",
 "importance":0..1,
 "confidence":0..1,
 "explicit":bool,
 "temporalNature":"permanent|current|temporary|historical|future",
 "validFrom":iso|null,"validUntil":iso|null,
 "sensitive":bool
}]}
```

Things worth stating in your wording, because the defaults will not match every
product:

- **What counts as durable.** "Still true next month" is the starter set's test.
- **`temporalNature`.** `permanent` protects a memory from ever being corrected,
  so it belongs only to facts that cannot change — a birthplace, a date of
  birth. Addresses, jobs and preferences are `current`.
- **`sensitive` discards the memory.** Name exactly what qualifies. In the
  starter set that is payment details, passwords and government ids — allergies
  and addresses are ordinary memories.
- **`importance`** ranks a memory in search and protects it from being
  forgotten. Give the model a scale with examples from your domain.

### decision

Decides what to do with one extracted fact against the memories MemDream already
holds for that user. Used in unified mode (the default).

**Receives:** the candidate memory, and up to ten related existing memories with
their ids, types, dates and similarity.

**Must return:**

```
{"action":"ADD|SUPERSEDE|MERGE|NOOP",
 "reason":str,
 "targetMemoryIds":[str],
 "supersededValidUntil":iso|null,
 "memory":{"content":str,"memoryType":str,"importance":0..1,
           "confidence":0..1,"temporalNature":str,
           "validFrom":iso|null,"validUntil":iso|null}|null}
```

The rule that matters most: **`targetMemoryIds` must be ids from the payload.**
MemDream drops any id it did not supply, so a hallucinated id is harmless — but
tell the model to echo them exactly, or it will paraphrase them and the
supersede will silently become an add.

### supersede

Staged mode only. Decides SUPERSEDE or NOOP, with the same payload and output
shape as `decision`. Also returns `supersededValidUntil`: when the old fact
stopped being true.

### merge

Staged mode only. Decides MERGE or NOOP, same payload and shape. `memory.content`
is the canonical text that replaces the originals, so it must lose nothing
either input had.

### synthesis

Decides whether a cluster of memories reveals a pattern none of them states
alone.

**Receives:** a cluster of related memories, plus the patterns that already
exist for that user.

**Must return:**

```
{"action":"SYNTHESIZE|NOOP","reason":str,
 "pattern":{"content":str,"confidence":0..1,"importance":0..1,
            "sourceMemoryIds":[str]}|null}
```

`sourceMemoryIds` is required and must be ids from the payload: a pattern
without evidence is rejected by the backend, not stored.

### compaction

Decides how to fold a crowd of near-identical low-value memories into one
summary. Only reached when compaction is enabled.

**Receives:** a cluster of low-importance memories.

**Must return:**

```
{"keep":bool, "summary":str|null, "reason":str}
```

`keep: false` means they do not belong together and should stay separate.
MemDream rejects a summary shorter than the longest memory it would replace, so
tell the model that dropping a fact is failure.

---

## Writing a good prompt

What the starter set learned, mostly from getting it wrong:

**One rule per line, imperative, no justification.** A model follows
*"preferences are never permanent"* just as well without three sentences
explaining why. Cutting the reasoning took the write path from about 2,150
tokens to about 500 with no measured change in behaviour — and you pay those
tokens on every message, from every user, forever.

**Every rule earns its place by fixing an observed failure.** Adding rules "to
be safe" costs tokens on every call and pulls attention from the rules that
matter.

**State what to do, not only what to avoid.** A rules block that only forbids
things stops the model doing the thing at all. If you tell it when *not* to
supersede, also tell it when to.

**Define your terms.** Removing the definition of `permanent` from a prompt
while keeping "preferences are never permanent" made the model mark addresses
permanent — which silently disabled superseding, because permanent memories are
never replaced.

**Be careful how narrow you make it.** A domain line or an extraction prompt
scoped tightly to your product will skip facts that only *imply* what you care
about: a delivery-focused prompt can pass over *"I moved to another city last
month"* while handling *"I live in Bangalore now."* correctly. Test relocation,
correction and contradiction phrasings specifically.

**Watch the token cost.** The starter prompts sit at roughly 200–320 tokens
each. `GET /v1/metrics/tokens` shows what yours actually cost per operation.

---

## Testing a change

**1. See what it does, offline.** No keys and no database:

```python
from mongomock_motor import AsyncMongoMockClient
from app.repositories.mongo import MongoDatabase
from memdream import MemDream

mem = MemDream(
    user_id="test",
    database=MongoDatabase(AsyncMongoMockClient(tz_aware=True), "scratch"),
    llm="openai:gpt-4o-mini",        # a real model, a throwaway database
    embeddings="openai:text-embedding-3-small",
    vector="memory",
    prompts="./prompts",
)

print(mem.add("I moved to Bangalore last month.").results)
```

**2. Check the decisions, not just the output.** The audit log records every
decision including the ones that stored nothing, with the reason:

```python
for op in mem.audit(limit=10):
    print(op["action"], "|", op["inputContent"], "->", op["reason"])
```

`NOOP` with a reason like *"No relevant user information to extract"* means the
extraction prompt did not recognise the fact — the most common failure, and
invisible if you only look at what was stored.

**3. Run the evaluation set.** 250 labelled cases covering extraction,
superseding, merging and tenancy:

```bash
python -m eval.run
```

**4. Watch the token cost.** `python -m scripts.calibrate` for similarity bands
after an embedding change; `GET /v1/metrics/tokens` for prompt cost after a
prompt change.
