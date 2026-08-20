"""Semantic Kernel integration.

    pip install semantic-kernel
    export AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_API_KEY=...
    python examples/semantic_kernel_agent.py

Two ways to wire Dream Memory into Semantic Kernel, and they are not
alternatives — use both.

**1. As a plugin (`DreamMemoryPlugin`).** Kernel functions the model can call
when it decides it needs them: "what do you know about my diet?" triggers
`recall`. Gives the model agency, at the cost of an extra round trip and the
model sometimes not bothering.

**2. As a filter (`MemoryContextFilter`).** Automatic recall before every
invocation and remembering after. Deterministic — memory is consulted whether or
not the model thinks to — and costs no extra model call.

The plugin handles "look something specific up"; the filter handles "always know
who you are talking to". Most assistants want the second and benefit from the
first.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path
from typing import Annotated

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memdream import AsyncDreamClient
from memdream.exceptions import DreamError

try:
    from semantic_kernel import Kernel
    from semantic_kernel.connectors.ai.function_choice_behavior import FunctionChoiceBehavior
    from semantic_kernel.connectors.ai.open_ai import (
        AzureChatCompletion,
        AzureChatPromptExecutionSettings,
    )
    from semantic_kernel.contents import ChatHistory
    from semantic_kernel.functions import kernel_function
except ImportError as exc:  # pragma: no cover - optional dependency
    print("This example needs semantic-kernel:\n\n    pip install semantic-kernel\n")
    raise SystemExit(1) from exc


DREAM_URL = os.environ.get("DREAM_BASE_URL", "http://localhost:8000")


# --------------------------------------------------------------------------- #
# 1. Memory as a plugin
# --------------------------------------------------------------------------- #


class DreamMemoryPlugin:
    """Kernel functions for recalling and recording long-term memory.

    The scope is bound at construction, from the authenticated session — never
    exposed as a function parameter. If the model could choose `clientId`, a
    prompt injection could read another tenant's memories, so it simply cannot
    name one.
    """

    def __init__(self, client: AsyncDreamClient) -> None:
        self._client = client

    @kernel_function(
        name="recall",
        description=(
            "Search the user's long-term memory. Use when the answer depends on "
            "something they told you before: preferences, personal details, "
            "constraints, ongoing projects."
        ),
    )
    async def recall(
        self,
        query: Annotated[str, "What to look for, in natural language."],
        include_history: Annotated[
            bool, "Include superseded memories to reason about how things changed."
        ] = False,
    ) -> Annotated[str, "Matching memories, one per line."]:
        try:
            results = await self._client.memory.search(
                query, limit=8, latest_only=not include_history
            )
        except DreamError as exc:
            return f"Memory is unavailable right now ({exc})."

        if not results:
            return "No relevant memories."

        lines = []
        for memory in results:
            suffix = "" if memory.is_active else f"  (no longer true: {memory.status})"
            lines.append(f"- {memory.content}{suffix}")
        return "\n".join(lines)

    @kernel_function(
        name="remember",
        description=(
            "Store something durable the user just told you about themselves. "
            "The memory layer decides whether this is new, replaces an older "
            "fact, or is already known — do not check first."
        ),
    )
    async def remember(
        self,
        fact: Annotated[str, "The fact, in the user's own words."],
    ) -> Annotated[str, "What the memory layer decided."]:
        try:
            result = await self._client.memory.add(fact)
        except DreamError as exc:
            return f"Could not store that ({exc})."

        if result.action == "NOOP":
            return "Already known; nothing stored."
        if result.action == "SUPERSEDE":
            return f"Stored, replacing {len(result.superseded_memory_ids)} outdated memory(ies)."
        if result.action == "MERGE":
            return "Stored, consolidated with what was already known."
        return "Stored."

    @kernel_function(
        name="recall_history",
        description=(
            "Show how a fact about the user changed over time, oldest to newest. "
            "Use for questions like 'where did they live before?'."
        ),
    )
    async def recall_history(
        self, query: Annotated[str, "The subject to trace."]
    ) -> Annotated[str, "The fact's history."]:
        try:
            results = await self._client.memory.search(
                query, limit=10, latest_only=False, include_superseded=True
            )
        except DreamError as exc:
            return f"Memory is unavailable right now ({exc})."

        if not results:
            return "No history found."

        lines = []
        for memory in results:
            window = ""
            if memory.valid_from:
                until = memory.valid_until.strftime("%Y-%m-%d") if memory.valid_until else "now"
                window = f"  [{memory.valid_from:%Y-%m-%d} to {until}]"
            lines.append(f"- {memory.content}{window}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 2. Memory as a filter
# --------------------------------------------------------------------------- #


class MemoryContextFilter:
    """Automatic recall before invocation, and remembering after.

    Registered as a function-invocation filter, so it runs for every call
    without the model having to decide to use memory.
    """

    def __init__(self, client: AsyncDreamClient, *, limit: int = 8) -> None:
        self._client = client
        self._limit = limit

    async def __call__(self, context, next):
        message = str(context.arguments.get("user_input", "")).strip()

        if message:
            try:
                results = await self._client.memory.search(
                    message, limit=self._limit, latest_only=True
                )
                context.arguments["memories"] = (
                    results.as_context() if results else "(nothing known yet)"
                )
            except DreamError:
                # Memory is an enhancement, never a hard dependency of a turn.
                context.arguments["memories"] = "(memory unavailable)"

        await next(context)

        if message:
            # After the model has answered, so the current message is never
            # context for answering itself.
            # A failed write costs the next turn some context; it must not
            # surface as an error on this one.
            with contextlib.suppress(DreamError):
                await self._client.memory.add(message)


# --------------------------------------------------------------------------- #
# Demo
# --------------------------------------------------------------------------- #

PROMPT = """\
You are a helpful assistant with long-term memory of this user.

What you already know:
{{$memories}}

Use it to personalise your answer. Do not recite it back unless asked, and never
claim to remember something that is not listed.

User: {{$user_input}}
Assistant:\
"""


def build_kernel(memory_client: AsyncDreamClient) -> Kernel:
    kernel = Kernel()

    kernel.add_service(
        AzureChatCompletion(
            service_id="chat",
            endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            deployment_name=os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o-mini"),
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        )
    )

    kernel.add_plugin(DreamMemoryPlugin(memory_client), plugin_name="memory")
    kernel.add_filter("function_invocation", MemoryContextFilter(memory_client))
    return kernel


async def main() -> None:
    if not (os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_API_KEY")):
        print("Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY to run this example.")
        return

    memory_client = AsyncDreamClient(
        base_url=DREAM_URL,
        client_id="client_demo",
        bot_id="bot_sk",
        phone_number="+919999999999",
    )

    try:
        await memory_client.health()
    except DreamError as exc:
        print(f"Dream Memory is not reachable at {DREAM_URL}: {exc}")
        print("Start it with: docker compose up")
        await memory_client.close()
        return

    kernel = build_kernel(memory_client)
    chat_function = kernel.add_function(
        plugin_name="assistant",
        function_name="chat",
        prompt=PROMPT,
        prompt_execution_settings=AzureChatPromptExecutionSettings(
            service_id="chat",
            temperature=0.7,
            # Lets the model call recall/remember on top of the automatic filter.
            function_choice_behavior=FunctionChoiceBehavior.Auto(),
        ),
    )

    history = ChatHistory()
    conversation = [
        "Hi, I'm Meera. I live in Chennai and work at Acme as a backend engineer.",
        "I'm vegetarian and allergic to peanuts.",
        "I moved to Bangalore last month.",
        "Where do I live, and what should I avoid eating?",
        "Where did I live before?",
    ]

    try:
        for message in conversation:
            print(f"\nUSER: {message}")
            result = await kernel.invoke(
                chat_function, user_input=message, memories="", chat_history=history
            )
            print(f"BOT:  {result}")

        print("\n" + "-" * 70)
        print("What the memory layer decided")
        print("-" * 70)
        for operation in await memory_client.memory.audit(limit=12):
            print(f"  {operation['action']:<10} {operation['reason'][:60]}")
    finally:
        await memory_client.close()


if __name__ == "__main__":
    asyncio.run(main())
