# Diagrams

Five views of MemDream, each answering one question. GitHub renders these
natively; paste any block into [mermaid.live](https://mermaid.live) to edit.

---

## 1. System architecture

*What are the moving parts?*

```mermaid
flowchart TB
    Agent["Your agent<br/>LangChain · Semantic Kernel · custom loop"]

    subgraph api["API process — uvicorn. The whole deployment."]
        Routes["REST API<br/>19 endpoints"]
        Engine["Dream Engine<br/>ADD · SUPERSEDE · MERGE · NOOP"]
        Retriever["Retriever<br/>filter then rank"]
        Lifecycle["Lifecycle<br/>the only writer of status"]
        Pool["Synthesis pool<br/>bounded background tasks"]
        Synth["Synthesis<br/>select · cluster · generalise"]
    end

    subgraph stores["Storage"]
        Mongo[("MongoDB or Postgres<br/><b>source of truth</b><br/>memories · relations<br/>audit · jobs")]
        Faiss[("FAISS<br/><b>derived index</b><br/>one partition per user")]
    end

    subgraph providers["Swappable providers"]
        LLM["LLM<br/>Azure OpenAI · fake"]
        Embed["Embeddings<br/>text-embedding-3-small · fake"]
    end

    Agent -->|"HTTP / SDK"| Routes
    Routes --> Engine
    Routes --> Retriever
    Engine --> Lifecycle
    Engine -.->|"reason"| LLM
    Engine -.->|"embed"| Embed
    Retriever -.-> Embed

    Lifecycle -->|"write"| Mongo
    Lifecycle -->|"index"| Faiss
    Retriever -->|"1 - eligible ids"| Mongo
    Retriever -->|"2 - rank those ids"| Faiss

    Routes -->|"job row, then a task"| Pool
    Pool --> Synth
    Synth --> Mongo
    Synth -.-> LLM

    Mongo -.->|"rebuild_index.py"| Faiss

    classDef store fill:#1e293b,stroke:#475569,color:#e2e8f0
    classDef proc fill:#1e3a5f,stroke:#3b82f6,color:#e2e8f0
    class Mongo,Faiss store
```

**Two key points.**

The database holds every embedding, so FAISS is disposable — delete it and
rebuild.

There is no second process and no broker. Synthesis is detached from the request
that triggered it by a bounded pool of tasks in the same process, which is all
"background" has to mean when the work is idempotent and its trigger is a
durable fact rather than an event. Set `DREAM_QUEUE_PROVIDER=database` to move
it to a worker instead — for several API replicas sharing one queue.

---

## 2. The write path

*What happens when a user says something?*

```mermaid
flowchart TB
    Start(["POST /v1/memories<br/>'I moved to Bangalore'"]) --> Extract

    Extract["<b>Extraction</b> — LLM call 1<br/>pull out durable facts"]
    Extract --> Policy{"<b>Policy</b><br/>confidence · importance<br/>sensitivity"}

    Policy -->|"rejected"| Drop(["Nothing stored<br/>reason logged"])
    Policy -->|"accepted"| Dup

    Dup{"<b>Exact duplicate?</b><br/>indexed hash lookup"}
    Dup -->|"yes"| Noop(["NOOP<br/><b>0 tokens</b> · ~40 ms"])
    Dup -->|"no"| Embed

    Embed["<b>Embed</b> the candidate"]
    Embed --> Candidates

    Candidates["<b>Find candidates</b><br/>Mongo filter → FAISS rank<br/>+ recent same-type"]
    Candidates -->|"none found"| Add(["ADD<br/>no decision call needed"])
    Candidates -->|"found some"| Decide

    Decide["<b>Decision</b> — LLM call 2<br/>ADD / SUPERSEDE / MERGE / NOOP"]
    Decide --> Validate

    Validate{"<b>Validation</b> — code only<br/>ids intersected with what<br/>the model was shown<br/>protected memories refused"}
    Validate --> Transition

    Transition["<b>Lifecycle</b><br/>Mongo + FAISS + edges"]
    Transition --> Audit(["Audit row<br/>action · reason · tokens"])

    classDef llm fill:#4c1d95,stroke:#a78bfa,color:#ede9fe
    classDef free fill:#064e3b,stroke:#34d399,color:#d1fae5
    class Extract,Decide llm
    class Noop,Dup free
```

**The cheap deterministic checks run first.** An exact repeat returns `NOOP`
having spent no embedding and no LLM call.

---

## 3. How it finds the contradiction

*The Chennai → Bangalore case, with real measured numbers.*

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant API as API
    participant M as MongoDB
    participant F as FAISS
    participant L as LLM

    U->>API: "I moved to Bangalore"
    API->>L: extract durable facts
    L-->>API: "User lives in Bangalore" (profile)

    Note over API,M: Step 1 — narrow the field, no LLM
    API->>M: eligible ids (scope + active + not pattern)
    M-->>API: 5 ids

    Note over API,F: Step 2 — rank only those 5, no LLM
    API->>F: search(vector, allowed_ids)
    F-->>API: Chennai 0.774 · Acme 0.396 · Postgres 0.376 · coffee 0.340 · dog 0.262

    Note over API: threshold 0.40 → 1 passes<br/>+ recent same-type adds Acme

    Note over API,L: Step 3 — the only expensive call
    API->>L: candidate + these 2 memories. contradiction?
    L-->>API: SUPERSEDE, target = Chennai id

    Note over API: validate — was that id one we showed it?

    API->>M: new row active · old row superseded + pointer
    API->>F: add new vector (old one stays — history is searchable)
    API-->>U: SUPERSEDE, supersededMemoryIds
```

**Why 0.774:** a contradiction is *lexically almost identical* to what it
contradicts. "User lives in Chennai" and "User lives in Bangalore" differ by one
token. That is exactly what makes it findable.

---

## 4. Synthesis — the funnel

*With 20 memories, what actually reaches the model?*

```mermaid
flowchart TB
    A["<b>20 memories</b> in store"] --> B

    B["<b>Candidate selection</b><br/>importance · type · confidence · recency<br/><i>deterministic, no LLM</i>"]
    B --> C["<b>20 candidates</b>"]

    C --> D["<b>Clustering</b><br/>agglomerative, average linkage<br/>cut-off 0.45<br/><i>no LLM</i>"]

    D --> E1["Cluster 1<br/>3 memories<br/>diet"]
    D --> E2["Cluster 2<br/>5 memories<br/>mornings"]
    D --> E3["Cluster 3<br/>4 memories<br/>fitness"]
    D --> E4["8 memories<br/>clustered with nothing"]

    E1 --> F1["LLM call 1"]
    E2 --> F2["LLM call 2"]
    E3 --> F3["LLM call 3"]
    E4 --> X(["<b>never sent</b><br/>0 tokens"])

    F1 --> G{"<b>Idempotency</b><br/>source overlap<br/>then semantic similarity"}
    F2 --> G
    F3 --> G

    G -->|"already covered"| Skip(["skip"])
    G -->|"new"| P(["<b>Pattern memory</b><br/>+ derived_from edges"])

    classDef llm fill:#4c1d95,stroke:#a78bfa,color:#ede9fe
    classDef free fill:#064e3b,stroke:#34d399,color:#d1fae5
    class F1,F2,F3 llm
    class B,D,X free
```

**Cost scales with clusters, not memories.** A user with 1,000 memories does not
cost 50× a user with 20 — the deterministic funnel caps what reaches the model.

---

## 5. Memory lifecycle

*Nothing is ever deleted by a Dream operation.*

```mermaid
stateDiagram-v2
    direction LR

    [*] --> ACTIVE: ADD

    ACTIVE --> SUPERSEDED: contradicted
    ACTIVE --> MERGED: consolidated
    ACTIVE --> EXPIRED: forgotten on request

    SUPERSEDED --> [*]: erase (operator only)
    MERGED --> [*]: erase (operator only)
    EXPIRED --> [*]: erase (operator only)

    note right of SUPERSEDED
        row KEPT in Mongo
        validUntil closed
        supersededBy set
        STAYS in FAISS
        history is searchable
    end note

    note right of MERGED
        row KEPT in Mongo
        mergedInto set
        REMOVED from FAISS
        content lives in canonical
    end note

    note right of EXPIRED
        row KEPT in Mongo
        REMOVED from FAISS
        reversible, auditable
    end note
```

Terminal states have no outgoing transitions. History is immutable — which is
also what makes a replayed operation converge instead of corrupting the chain.

| Status | In Mongo | In FAISS |
|---|---|---|
| `active` | yes | yes |
| `superseded` | yes | **yes** — history is searchable |
| `merged` | yes | no — content is in the canonical memory |
| `expired` | yes | no |
| erased | no | no |
