"""Integrating Dream Memory into a custom agent.

    # terminal 1
    docker compose up

    # terminal 2
    export AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_API_KEY=...
    python examples/custom_agent.py

The pattern is three lines in the turn loop, and works for any framework:

    memories = client.memory.search(user_message, latest_only=True)   # recall
    reply    = llm(system + memories.as_context(), user_message)      # respond
    client.memory.add(user_message)                                   # remember

Two decisions in here are worth copying:

**Recall before responding, remember after.** Remembering first would let the
message the user just sent come back as "context" for answering itself.

**Remembering does not block the reply.** A memory write costs an extraction
call and possibly a decision call. The user should not wait for that, so it goes
to a background thread and a failure degrades the next turn rather than breaking
this one.
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sdk"))

from memdream import DreamClient
from memdream.exceptions import DreamError

DREAM_URL = os.environ.get("DREAM_BASE_URL", "http://localhost:8000")

SYSTEM_PROMPT = """\
You are a helpful assistant with long-term memory of this user.

Memories below were learned in earlier conversations. Use them to personalise
your answer. Do not recite them back unless asked, and do not claim to remember
anything that is not listed. If a memory is marked as superseded, it describes
the past, not the present.\
"""


class MemoryAugmentedAgent:
    def __init__(
        self,
        *,
        client_id: str,
        bot_id: str,
        phone_number: str,
        base_url: str = DREAM_URL,
        api_key: str | None = None,
    ) -> None:
        self.memory = DreamClient(
            base_url=base_url,
            api_key=api_key,
            client_id=client_id,
            bot_id=bot_id,
            phone_number=phone_number,
        )
        # Writes happen off the request path. A single worker keeps them ordered,
        # which matters: two messages from one turn must not race each other into
        # the Dream engine and both decide "nothing related exists".
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dream-write")
        self.history: list[dict[str, str]] = []

    # --- recall ------------------------------------------------------------- #

    def recall(self, message: str, limit: int = 8) -> str:
        """Fetch relevant memories as prompt context.

        `latest_only=True` because an agent answering a question wants what is
        true now. Drop it to let the model reason about how things changed.
        """
        try:
            results = self.memory.search(message, limit=limit, latest_only=True)
        except DreamError as exc:
            # Memory is an enhancement. A memory outage degrades the assistant
            # to a stateless one; it must not take the conversation down.
            print(f"  [memory unavailable: {exc}]", file=sys.stderr)
            return ""

        if not results:
            return ""
        return "\n\n## What you know about this user\n" + results.as_context()

    # --- remember ----------------------------------------------------------- #

    def remember(self, message: str, message_id: str | None = None) -> None:
        """Queue the message for extraction, without blocking the reply."""

        def _write() -> None:
            try:
                result = self.memory.add(message, message_id=message_id)
                if result.remembered:
                    for decision in result.results:
                        print(
                            f"  [memory: {decision.action} {decision.content or ''}]",
                            file=sys.stderr,
                        )
            except DreamError as exc:
                print(f"  [memory write failed: {exc}]", file=sys.stderr)

        self._writer.submit(_write)

    # --- turn --------------------------------------------------------------- #

    def chat(self, message: str) -> str:
        context = self.recall(message)
        reply = self._generate(SYSTEM_PROMPT + context, message)

        self.history.append({"role": "user", "content": message})
        self.history.append({"role": "assistant", "content": reply})

        # After generating: the current message must not be context for itself.
        self.remember(message)
        return reply

    def _generate(self, system: str, message: str) -> str:
        """Call your model here.

        Swapped for a stub when no credentials are present, so the memory
        behaviour can be demonstrated without an LLM deployment.
        """
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
        api_key = os.environ.get("AZURE_OPENAI_API_KEY")
        if not (endpoint and api_key):
            has_context = "## What you know" in system
            return (
                "[no LLM configured] I would answer using "
                + ("the memories recalled above." if has_context else "no prior context.")
            )

        from openai import AzureOpenAI

        client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        )
        response = client.chat.completions.create(
            model=os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": system},
                *self.history[-6:],
                {"role": "user", "content": message},
            ],
            temperature=0.7,
        )
        return response.choices[0].message.content or ""

    # --- background maintenance --------------------------------------------- #

    def dream(self, wait: bool = False):
        """Trigger synthesis.

        In production this is a scheduled worker job, not something the agent
        calls. Exposed here so the effect is visible in the demo.
        """
        return self.memory.dream.synthesize(wait=wait)

    def close(self) -> None:
        self._writer.shutdown(wait=True)
        self.memory.close()


def main() -> None:
    agent = MemoryAugmentedAgent(
        client_id="client_demo",
        bot_id="bot_demo",
        phone_number="+919999999999",
    )

    try:
        agent.memory.health()
    except DreamError as exc:
        print(f"Dream Memory is not reachable at {DREAM_URL}: {exc}")
        print("Start it with: docker compose up")
        return

    conversation = [
        "Hi! I'm Meera, I live in Chennai and I work at Acme as a backend engineer.",
        "I'm vegetarian and allergic to peanuts.",
        "Where do I live?",
        "I moved to Bangalore last month.",
        "Where do I live now?",
        "What do you know about my diet?",
    ]

    try:
        for message in conversation:
            print(f"\nUSER: {message}")
            print(f"BOT:  {agent.chat(message)}")

        # Let the queued writes land before asking what was stored.
        agent._writer.shutdown(wait=True)
        agent._writer = ThreadPoolExecutor(max_workers=1)

        print("\n" + "-" * 70)
        print("Stored memories")
        print("-" * 70)
        for memory in agent.memory.list(status=["active", "superseded"]):
            marker = "" if memory.is_active else f"  [{memory.status}]"
            print(f"  {memory.content}{marker}")

        print("\n" + "-" * 70)
        print("Why each decision was made")
        print("-" * 70)
        for operation in agent.memory.audit(limit=10):
            print(f"  {operation['action']:<10} {operation['reason'][:60]}")
    finally:
        agent.close()


if __name__ == "__main__":
    main()
