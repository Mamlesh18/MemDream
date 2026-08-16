"""Dream Memory Python SDK.

    from memdream import DreamClient

    client = DreamClient(base_url="http://localhost:8000")

    client.memory.add(
        prompt="I moved to Bangalore.",
        phone_number="+919999999999",
        bot_id="bot_123",
        client_id="client_456",
    )

An async client with the same surface is available as `AsyncDreamClient`.
"""

from memdream.client import AsyncDreamClient, DreamClient
from memdream.exceptions import (
    DreamAPIError,
    DreamAuthError,
    DreamConnectionError,
    DreamNotFoundError,
    DreamValidationError,
)
from memdream.models import (
    AddResult,
    DreamJob,
    MemoryDetail,
    MemoryRecord,
    SearchResult,
)

__version__ = "0.1.0"

__all__ = [
    "AddResult",
    "AsyncDreamClient",
    "DreamAPIError",
    "DreamAuthError",
    "DreamClient",
    "DreamConnectionError",
    "DreamJob",
    "DreamNotFoundError",
    "DreamValidationError",
    "MemoryDetail",
    "MemoryRecord",
    "SearchResult",
]
