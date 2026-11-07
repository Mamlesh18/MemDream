# Changelog

All notable changes to MemDream are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) — while MemDream is `0.x`, a minor
version may contain breaking changes, and they are always listed.

Install a specific release by tag:

```bash
pip install "memdream @ git+https://github.com/Mamlesh18/MemDream.git@v0.1.0"
```

## [Unreleased]

## [0.1.0] - 2026-11-07

First tagged release.

### Added

- **A library first.** `MemDream` / `AsyncMemDream` run the whole engine inside
  your process — no server, no worker. `DreamClient` / `AsyncDreamClient` offer
  the same methods over HTTP for the optional shared service.
- **Contradiction-aware memory.** Every new fact is decided as ADD, SUPERSEDE,
  MERGE or NOOP against what is already known, using prompts you write.
  Superseded and merged memories are kept as history with pointers to their
  replacements.
- **Dream, automatically** (`auto_dream=True`): synthesis of pattern memories
  with links to their evidence, and a daily pass that retires memories whose
  validity has ended. Compaction and decay are available, off by default.
- **Manual control:** `create`, `update`, `forget` (reversible), `delete`
  (permanent), `purge` (everything for one user).
- **Inspection:** `get`, `list`, `history`, `audit` (every decision, NOOPs
  included), `stats` (counts, tokens, estimated cost).
- `database="memory"`: an in-process store, to try MemDream with no database.
- Providers: OpenAI, Azure OpenAI, Anthropic, Gemini, Ollama, Together,
  Fireworks and any OpenAI-compatible endpoint; MongoDB or PostgreSQL; FAISS,
  Qdrant, pgvector, Chroma, Pinecone, Milvus or Azure AI Search.
- `python -m memdream prompts ./prompts` copies a working starter prompt set.
- Security policy, code of conduct, issue templates and CI (Python 3.10–3.13,
  integration suites against real MongoDB, pgvector and Qdrant).

### Upgrade notes — if you used MemDream from `main` before this release

- **The engine moved from `app` to `memdream.engine`.** Installing MemDream no
  longer creates a top-level `app` package. Update direct imports
  (`from app.core.config import Settings` →
  `from memdream.engine.core.config import Settings`) and commands
  (`uvicorn app.main:app` → `uvicorn memdream.engine.main:app`;
  `python -m app.workers.synthesis_worker` →
  `python -m memdream.engine.workers.synthesis_worker`). Code using only
  `from memdream import ...` is unaffected.
- **Phone-number identity needs a private salt.** Deriving a user id from a phone
  number now fails while `DREAM_IDENTITY_SALT` is unset or a published
  placeholder (including the old built-in default). If you already stored
  memories under ids derived with the default salt, setting a real salt gives
  those users new ids, so their existing memories would need re-keying to stay
  reachable. Callers passing `user_id` are unaffected.
- **The Groq provider was removed.** Use another OpenAI-compatible provider, or
  `openai_compatible` with Groq's base URL.
- **`delete()` now redacts the audit log.** Rows referencing a permanently
  deleted memory keep their ids and timestamps but lose `inputContent` and
  `reason`.
- Library mode now logs warnings only, to stderr; pass `log_level="INFO"` for
  the previous detail.

[Unreleased]: https://github.com/Mamlesh18/MemDream/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Mamlesh18/MemDream/releases/tag/v0.1.0
