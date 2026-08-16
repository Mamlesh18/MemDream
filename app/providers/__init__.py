"""Swappable providers: LLM, embeddings, vector store, job queue.

Nothing in `app.memory` or `app.dream` imports a concrete provider. They take
the abstract base classes defined here, which is what makes Azure OpenAI the
first implementation rather than an architectural dependency.
"""
