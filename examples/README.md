# Examples

## `embedded.py` — MemDream with no server

```bash
python examples/embedded.py
```

The shortest possible integration: three lines, no uvicorn, no worker, no API
keys, no cost.

```python
from memdream import MemDream

mem = MemDream(user_id="alice")
mem.add("I moved to Bangalore.")
print(mem.search("Where does the user live?").as_context())
```

`MemDream` builds the same container the API builds, so contradiction handling,
tenant isolation and synthesis behave exactly as they do behind HTTP. When you
later put MemDream behind a server, swap the constructor for `DreamClient` and
leave the rest of your code alone — the method names, arguments and return types
are the same.

Start here if you want memory working in the next five minutes.
`quickstart.py` below goes deeper into *why* each decision comes out the way it
does.

---

## `quickstart.py` — no infrastructure

```bash
python examples/quickstart.py
```

Runs the entire framework in-process with the offline providers: no MongoDB, no
Redis, no Azure credentials, no network, no cost. Walks through every behaviour
the system exists to provide — add, exact-duplicate NOOP, supersede,
non-destructive history, current-vs-historical retrieval, synthesis,
idempotency, tenant isolation, and the audit trail.

Start here. It is the fastest way to see what the framework actually does, and
the output doubles as a specification of the expected behaviour.

---

## `custom_agent.py` — any agent framework

```bash
docker compose up                    # terminal 1
python examples/custom_agent.py      # terminal 2
```

The integration pattern in three lines:

```python
memories = client.memory.search(user_message, latest_only=True)   # recall
reply    = llm(system + memories.as_context(), user_message)      # respond
client.memory.add(user_message)                                   # remember
```

Two things in this example are worth copying into your own agent:

- **Recall before responding, remember after.** Remembering first lets the
  message the user just sent come back as "context" for answering itself.
- **Remembering does not block the reply.** A memory write costs an extraction
  call and possibly a decision call. It goes to a background worker, and a
  failure degrades the next turn rather than breaking this one.

Runs without an LLM deployment (the generation step falls back to a stub), so
you can watch the memory decisions on their own.

---

## `semantic_kernel_agent.py` — Semantic Kernel

```bash
pip install semantic-kernel
export AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_API_KEY=...
python examples/semantic_kernel_agent.py
```

Shows both integration points, which are complements rather than alternatives:

| | What it does | Trade-off |
|---|---|---|
| `DreamMemoryPlugin` | kernel functions `recall`, `remember`, `recall_history` the model calls when it decides it needs them | gives the model agency; costs a round trip, and the model sometimes does not bother |
| `MemoryContextFilter` | automatic recall before every invocation, remembering after | deterministic and free; no model agency |

The plugin handles "look something specific up". The filter handles "always know
who you are talking to". Most assistants want the filter and benefit from the
plugin.

Note that the plugin binds the scope at construction, from the authenticated
session — it is never a function parameter. If the model could choose
`clientId`, a prompt injection could read another tenant's memories. It simply
cannot name one.

---

## Notes for real deployments

**Scope comes from your session, never from the model or the user.** Both SDK
forms let you set it once:

```python
client = DreamClient(
    base_url="http://localhost:8000",
    client_id=session.client_id,
    agent_id=session.agent_id,
    phone_number=session.phone_number,
)

mem = MemDream(
    client_id=session.client_id,
    agent_id=session.agent_id,
    phone_number=session.phone_number,
)
```

Only `user_id` or `phone_number` is required; `agent_id`, `client_id` and
`namespace` are optional dimensions you add if your deployment has them.

**Hosted, bind your API keys to a tenant.** `DREAM_API_KEY_CLIENTS=key:acme`
makes `clientId` come from the credential rather than the request body, so a
caller cannot name someone else's tenant. Without it, any valid key can.

**Embedded, build one `MemDream` per process and keep it.** It holds a database
pool, an embedding client and an LLM client. One per request works but pays
connection setup every time, and will exhaust the database's connection limit
under load.

**Synthesis is a scheduled job, not something an agent calls.** Enable the
worker's nightly sweep with `DREAM_DREAM__SYNTHESIS__SCHEDULE_ENABLED=true`, or
trigger it from your own scheduler via `POST /v1/dream/synthesize`. The examples
call it directly only so the effect is visible.

**Memory is an enhancement, not a hard dependency.** Both examples catch
`DreamError` and continue without context. An agent that cannot answer because
the memory service is down is worse than one that answers without memory.
