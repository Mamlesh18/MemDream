"""The MemDream Python SDK.

Two ways in, one surface.

**Embedded** — MemDream runs inside your process. No server, no worker::

    from memdream import MemDream

    mem = MemDream(user_id="alice")
    mem.add("I moved to Bangalore last month.")
    print(mem.search("where do they live?").as_context())

**Hosted** — talk to a running MemDream over HTTP::

    from memdream import DreamClient

    mem = DreamClient("https://memory.internal", api_key="...", user_id="alice")
    mem.add("I moved to Bangalore last month.")

The method names, arguments and return types are identical, so moving from a
prototype to a deployed service changes the constructor line and nothing else.
Both also answer to `.memory` and `.dream` for the namespaced call style.

Async equivalents are `AsyncMemDream` and `AsyncDreamClient`.

`DreamClient` needs only `httpx`. `MemDream` needs the full package, since it
builds the engine rather than calling it.
"""

from memdream.client import AsyncDreamClient, DreamClient
from memdream.embedded import AsyncMemDream, MemDream
from memdream.exceptions import (
    DreamAPIError,
    DreamAuthError,
    DreamConnectionError,
    DreamNotFoundError,
    DreamValidationError,
)
from memdream.models import (
    AddResult,
    DecisionResult,
    DreamJob,
    MemoryDetail,
    MemoryRecord,
    RelatedMemory,
    SearchResult,
)

__version__ = "0.1.0"

__all__ = [
    "AddResult",
    "AsyncDreamClient",
    "AsyncMemDream",
    "DecisionResult",
    "DreamAPIError",
    "DreamAuthError",
    "DreamClient",
    "DreamConnectionError",
    "DreamJob",
    "DreamNotFoundError",
    "DreamValidationError",
    "MemDream",
    "MemoryDetail",
    "MemoryRecord",
    "RelatedMemory",
    "SearchResult",
]
