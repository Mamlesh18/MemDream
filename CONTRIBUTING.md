# Contributing to MemDream

Thanks for considering it. This document covers how to get set up, what we look
for in a change, and the few invariants that are not negotiable.

---

## Getting set up

```bash
git clone https://github.com/your-org/memdream.git
cd memdream

python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate

pip install -e ".[dev]"
```

That is enough to run everything:

```bash
pytest                 # 246 tests, fully offline
python -m eval.run     # 250-case evaluation set
ruff check app sdk eval scripts tests examples
```

**No credentials, no database and no network are needed to develop.** Every
provider has an offline implementation, so `pytest` runs against fakes. If you
find yourself needing a live service to test something, that is usually a sign
the code should take an interface rather than construct a provider.

To run against real infrastructure:

```bash
cp .env.example .env            # fill in AZURE_OPENAI_*
docker compose up               # MongoDB + API. That is the whole stack

DREAM_TEST_MONGODB_URI=mongodb://localhost:27017 pytest   # against real Mongo
python -m eval.run --provider azure                       # against a real model
```

---

## The offline fakes are not stubs

This surprises people, so it is worth stating plainly.

`FakeEmbeddingProvider` is a feature-hashing bag-of-words model — related texts
genuinely score higher than unrelated ones. `FakeLLMProvider` implements real
rules: change-frame detection, single-valued predicate conflicts,
containment-based enrichment, source-overlap idempotency. It scores **84.4%** on
the evaluation set.

That is deliberate. A stub returning `ADD` for everything would let every test
pass while exercising nothing. If you add a code path, the fakes should be able
to reach it — and if making them reach it is hard, say so in the PR rather than
weakening the test.

---

## What we look for

**Tests that would fail without your change.** Not coverage for its own sake —
a test whose name states a property, and which breaks if that property breaks.

**Comments that explain *why*, not *what*.** The code says what it does. The
comment should say why it does it that way and what breaks otherwise. Compare:

```python
# Bad — restates the code
# Set the status to superseded
memory.status = MemoryStatus.SUPERSEDED

# Good — explains a decision a reader would otherwise question
# The new memory is written first. If the process dies between the two writes,
# the result is a duplicate active memory — recoverable, and visible to the next
# Dream pass. The reverse order would leave the user with neither fact.
```

**Honest reporting.** If your change makes one metric better and another worse,
say so in the PR. We would much rather know.

---

## Invariants — please do not break these

These are the properties the whole design rests on. A change that violates one
needs a very good argument.

### 1. Nothing is deleted by a Dream operation

`SUPERSEDE` and `MERGE` transition status and write a pointer. They never remove
a row. Deletion exists only as a deliberate operator action
(`DELETE /v1/memories/{id}`), never as a consequence of reasoning.

### 2. Terminal states are terminal

`superseded`, `merged` and `expired` have no outgoing transitions. History is
immutable — this is also what makes a replayed operation converge instead of
corrupting the chain.

### 3. Every repository method takes a `MemoryScope`

There is no method that reads or writes a memory without one. This is why a
caller cannot forget a tenancy filter: there is nothing to forget. If you are
adding a query, it starts from `scope.as_filter()`.

### 4. The model proposes; the backend decides

Target ids returned by a model are **intersected with the ids that model was
shown**. A hallucinated id is dropped, not looked up. This is what makes it
impossible for a model to reach another tenant's memory by guessing an id — the
id never becomes a query. Do not add a path that looks up an id straight from
model output.

### 5. `app/memory` and `app/dream` import provider *base classes* only

Never a concrete provider. This is the invariant that keeps Azure OpenAI a first
implementation rather than a dependency. Construction happens in
`app/container.py`, the composition root.

### 6. Synthesis is idempotent and additive

Running it twice over the same evidence must not create a second pattern, and it
must never modify a source memory.

---

## Especially welcome

**A new provider adapter.** OpenAI, Ollama, Cohere, HuggingFace, Qdrant,
pgvector, Azure AI Search, Celery, SQS. Implement the base class, add one line to
the factory, add tests. The interfaces are small on purpose — `VectorStore` is
five methods, `JobQueue` is three.

**Extraction prompts for a new vertical.** Healthcare, recruiting, e-commerce,
education. What is worth remembering varies enormously by domain, and
`EXTRACTION_SYSTEM` is currently tuned for a general assistant.

**Evaluation cases.** Especially ones that expose a failure we do not handle.
A case that fails today is a genuinely useful contribution — open it as an issue
even if you do not have a fix.

**Threshold calibration for other embedding models.** `scripts/calibrate.py`
measures where the similarity bands actually fall. We have measured
`text-embedding-3-small`; numbers for other models would help everyone.

---

## Prompt changes

Prompts live in one file, [`app/dream/prompts.py`](app/dream/prompts.py). They
are the highest-leverage code in the project and the easiest to get subtly wrong.

Before and after any prompt change:

```bash
python -m eval.run --report before.json
# ... make the change ...
python -m eval.run --report after.json
```

Include both numbers in the PR. A prompt change that improves one suite and
degrades another is a trade-off worth discussing, not a blocker — but it needs to
be visible.

Bear in mind the three layers. The **contract** (output schema) and **safety**
rules (never invent facts, preserve history, never lose information) protect data
integrity. The **domain** guidance is where customisation belongs.

---

## Pull requests

1. Fork, branch from `main`.
2. Make the change, with tests.
3. `pytest && ruff check app sdk eval scripts tests examples`
4. If you touched prompts or thresholds, include before/after eval numbers.
5. Open the PR describing **what problem it solves**, not just what it does.

Small, focused PRs get reviewed quickly. A large one is fine too — just say up
front what it covers so it can be reviewed in pieces.

### Commit messages

Plain and descriptive. `fix: supersede refused on mislabelled permanent
preferences` beats `fix bug`.

---

## Reporting bugs

Please include:

- what you expected and what happened
- the relevant audit rows — `GET /v1/memories/audit/operations` includes the
  reason for every decision and any correction the backend made to the model's
  proposal, which is usually the fastest route to the cause
- your provider and model
- whether it reproduces with `DREAM_LLM_PROVIDER=fake`

That last one is genuinely useful: if it reproduces offline, we can write a test
for it.

### Security issues

Please do **not** open a public issue for anything involving cross-tenant data
access, credential exposure, or prompt injection that escapes a scope. Email the
maintainers instead so it can be fixed before it is described publicly.

---

## Code of conduct

Be decent to each other. Assume good faith, critique the code rather than the
person, and remember that the person on the other end may be new to this, working
in a second language, or both.

Behaviour that makes people not want to participate — harassment, personal
attacks, sustained dismissiveness — is not welcome, and maintainers will act on it.

---

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), the same terms as the project.
