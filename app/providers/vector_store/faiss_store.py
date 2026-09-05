"""FAISS vector store, partitioned per (client, bot, user).

Design notes
------------

**One index per scope.** FAISS has no metadata filter, so a single shared index
would mean fetching neighbours and *then* discarding other tenants' rows — a
design where a filtering bug becomes a data leak. Partitioning physically makes
cross-tenant retrieval unrepresentable, and keeps each index small enough that
an exact (flat) search is cheap.

**Exact search.** `IndexFlatIP` over L2-normalised vectors is cosine similarity
with no approximation. Per-user memory counts are in the hundreds-to-thousands,
where a flat scan beats an approximate index on both accuracy and build cost.

**Two processes write.** The API and the synthesis worker both mutate indexes.
Correctness comes from an inter-process `FileLock` around every read-modify-write
plus an mtime check that reloads a partition another process has changed. Writes
land via a temp file and an atomic replace, so a crash mid-write cannot leave a
torn index.

**FAISS is synchronous.** All index work is pushed to a thread so the event loop
keeps serving.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np
from filelock import FileLock

from app.core.exceptions import ProviderError
from app.core.logging import get_logger
from app.core.metrics import VECTOR_SEARCH_LATENCY, VECTOR_SEARCHES, metrics
from app.models.scope import MemoryScope
from app.providers.vector_store.base import VectorHit, VectorRecord, VectorStore

logger = get_logger(__name__)

#: Below this many vectors a partition is scanned exhaustively on search, which
#: makes `allowed_ids` post-filtering exact. Above it we over-fetch instead.
_EXHAUSTIVE_SCAN_LIMIT = 4000
#: Multiplier applied to top_k when over-fetching a large partition.
_OVERFETCH_FACTOR = 8


class _Partition:
    """One scope's index plus the id mapping FAISS itself cannot store.

    FAISS addresses vectors by int64. Memory ids are strings, so the mapping
    between them is kept in a JSON sidecar written atomically alongside the index.
    """

    __slots__ = ("key", "index", "id_to_memory", "memory_to_id", "next_id", "dim", "mtime")

    def __init__(self, key: str, dim: int) -> None:
        self.key = key
        self.dim = dim
        self.index: Any = None
        self.id_to_memory: dict[int, str] = {}
        self.memory_to_id: dict[str, int] = {}
        self.next_id = 1
        self.mtime = 0.0


class FaissVectorStore(VectorStore):
    def __init__(
        self,
        path: str = "./data/faiss",
        dimensions: int = 1536,
        max_loaded_indexes: int = 128,
    ) -> None:
        try:
            import faiss  # noqa: F401
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ProviderError(
                "faiss-cpu is required for the FAISS vector store. "
                "Install it, or set DREAM_VECTOR_PROVIDER=memory for local runs."
            ) from exc

        self._root = Path(path)
        self._root.mkdir(parents=True, exist_ok=True)
        self._dimensions = dimensions
        self._max_loaded = max(1, max_loaded_indexes)

        self._loaded: OrderedDict[str, _Partition] = OrderedDict()
        #: One asyncio lock per partition serialises coroutines in this process;
        #: the FileLock below serialises across processes.
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "faiss"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    # --- paths -------------------------------------------------------------- #

    def _slug(self, scope: MemoryScope) -> str:
        """Filesystem-safe, collision-free partition name.

        Hashed rather than sanitised because client and bot ids may contain
        characters Windows forbids in filenames, and because a hash keeps
        tenant identifiers out of the filesystem.
        """
        digest = hashlib.sha256(scope.partition_key.encode("utf-8")).hexdigest()
        return digest[:40]

    def _paths(self, slug: str) -> tuple[Path, Path, Path]:
        base = self._root / slug
        return (
            base.with_suffix(".index"),
            base.with_suffix(".meta.json"),
            base.with_suffix(".lock"),
        )

    async def _lock_for(self, slug: str) -> asyncio.Lock:
        async with self._registry_lock:
            lock = self._locks.get(slug)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[slug] = lock
            return lock

    # --- load / save (blocking; always called via asyncio.to_thread) --------- #

    def _new_index(self, dim: int) -> Any:
        import faiss

        # IDMap2 lets us address by our own int64 ids and supports removal.
        return faiss.IndexIDMap2(faiss.IndexFlatIP(dim))

    def _load_blocking(self, slug: str, dim: int) -> _Partition:
        import faiss

        index_path, meta_path, _ = self._paths(slug)
        partition = _Partition(slug, dim)

        if index_path.is_file() and meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                partition.index = faiss.read_index(str(index_path))
                partition.id_to_memory = {
                    int(k): v for k, v in meta.get("idToMemory", {}).items()
                }
                partition.memory_to_id = {v: k for k, v in partition.id_to_memory.items()}
                partition.next_id = int(meta.get("nextId", 1))
                partition.dim = int(meta.get("dim", dim))
                partition.mtime = meta_path.stat().st_mtime
                return partition
            except Exception as exc:  # noqa: BLE001
                # A corrupt index is recoverable: Mongo holds every embedding, so
                # the operator can rebuild. Starting empty beats refusing to boot.
                logger.error(
                    "faiss_partition_corrupt",
                    partition=slug,
                    error=str(exc)[:300],
                    hint="rebuild with scripts/rebuild_index.py",
                )

        partition.index = self._new_index(dim)
        return partition

    @staticmethod
    def _replace(source: Path, target: Path, *, attempts: int = 5) -> None:
        """`os.replace`, retried past a transient Windows lock.

        The rename is atomic on both platforms, which is the whole reason for
        the temp file. On Windows it can still fail with `Access is denied` for
        a reason that has nothing to do with this process: a virus scanner or
        the search indexer opens a file the moment it is written, and holds it
        for a few milliseconds. `os.replace` cannot overwrite a target another
        handle has open, so the write fails and — without this — takes the
        caller's request with it.

        Retrying briefly is the standard remedy. Anything still failing after a
        second is a real permission problem and should surface as one.
        """
        for attempt in range(attempts):
            try:
                os.replace(source, target)
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                logger.debug("faiss_replace_retry", target=target.name, attempt=attempt + 1)
                time.sleep(0.05 * (attempt + 1))

    def _save_blocking(self, partition: _Partition) -> None:
        import faiss

        index_path, meta_path, _ = self._paths(partition.key)

        # Write to a sibling temp file then rename, so readers never observe a
        # half-written index.
        tmp_index = index_path.with_suffix(".index.tmp")
        faiss.write_index(partition.index, str(tmp_index))
        self._replace(tmp_index, index_path)

        tmp_meta = meta_path.with_suffix(".json.tmp")
        tmp_meta.write_text(
            json.dumps(
                {
                    "dim": partition.dim,
                    "nextId": partition.next_id,
                    "idToMemory": {str(k): v for k, v in partition.id_to_memory.items()},
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        self._replace(tmp_meta, meta_path)
        partition.mtime = meta_path.stat().st_mtime

    def _needs_reload(self, partition: _Partition) -> bool:
        """True when another process has written this partition since we loaded."""
        _, meta_path, _ = self._paths(partition.key)
        try:
            return meta_path.stat().st_mtime > partition.mtime
        except FileNotFoundError:
            return False

    async def _acquire(self, scope: MemoryScope, dim: int | None = None) -> _Partition:
        slug = self._slug(scope)
        dim = dim or self._dimensions

        partition = self._loaded.get(slug)
        if partition is not None and not await asyncio.to_thread(self._needs_reload, partition):
            self._loaded.move_to_end(slug)
            return partition

        partition = await asyncio.to_thread(self._load_blocking, slug, dim)
        self._loaded[slug] = partition
        self._loaded.move_to_end(slug)

        while len(self._loaded) > self._max_loaded:
            # Evicting only drops the in-memory copy; state is already on disk.
            evicted_key, _ = self._loaded.popitem(last=False)
            self._locks.pop(evicted_key, None)
        return partition

    # --- write path ---------------------------------------------------------- #

    def _apply_upsert_blocking(self, partition: _Partition, records: list[VectorRecord]) -> None:
        import faiss

        # Replacing an existing vector means removing the old id first: IDMap2
        # would otherwise hold two entries for one memory.
        replacing = [
            partition.memory_to_id[record.memory_id]
            for record in records
            if record.memory_id in partition.memory_to_id
        ]
        if replacing:
            partition.index.remove_ids(np.asarray(replacing, dtype=np.int64))

        vectors = np.zeros((len(records), partition.dim), dtype=np.float32)
        ids = np.zeros(len(records), dtype=np.int64)

        for row, record in enumerate(records):
            faiss_id = partition.memory_to_id.get(record.memory_id)
            if faiss_id is None:
                faiss_id = partition.next_id
                partition.next_id += 1
                partition.memory_to_id[record.memory_id] = faiss_id
                partition.id_to_memory[faiss_id] = record.memory_id
            vectors[row] = np.asarray(record.vector, dtype=np.float32)
            ids[row] = faiss_id

        # Belt and braces: the provider normalises, but a caller-supplied vector
        # (e.g. an index rebuild from old data) might not be.
        faiss.normalize_L2(vectors)
        partition.index.add_with_ids(vectors, ids)

    async def upsert(self, scope: MemoryScope, records: list[VectorRecord]) -> None:
        if not records:
            return

        dim = len(records[0].vector)
        for record in records:
            if len(record.vector) != dim:
                raise ProviderError(
                    f"Inconsistent embedding dimensions in one batch: "
                    f"{dim} vs {len(record.vector)}"
                )

        slug = self._slug(scope)
        _, _, lock_path = self._paths(slug)
        async with await self._lock_for(slug):
            with FileLock(str(lock_path), timeout=30):
                partition = await self._acquire(scope, dim)
                if partition.index.ntotal == 0 and partition.dim != dim:
                    # An empty partition adopts the first dimension it sees, so
                    # switching embedding models only requires clearing indexes.
                    partition.dim = dim
                    partition.index = await asyncio.to_thread(self._new_index, dim)
                if partition.dim != dim:
                    raise ProviderError(
                        f"Embedding dimension {dim} does not match this partition's "
                        f"{partition.dim}. Rebuild the index after changing model."
                    )
                await asyncio.to_thread(self._apply_upsert_blocking, partition, records)
                await asyncio.to_thread(self._save_blocking, partition)

    async def update(self, scope: MemoryScope, records: list[VectorRecord]) -> None:
        await self.upsert(scope, records)

    async def delete(self, scope: MemoryScope, memory_ids: list[str]) -> None:
        if not memory_ids:
            return
        slug = self._slug(scope)
        _, _, lock_path = self._paths(slug)

        async with await self._lock_for(slug):
            with FileLock(str(lock_path), timeout=30):
                partition = await self._acquire(scope)
                targets = [
                    partition.memory_to_id[memory_id]
                    for memory_id in memory_ids
                    if memory_id in partition.memory_to_id
                ]
                if not targets:
                    return

                def _remove() -> None:
                    partition.index.remove_ids(np.asarray(targets, dtype=np.int64))
                    for faiss_id in targets:
                        memory_id = partition.id_to_memory.pop(faiss_id, None)
                        if memory_id is not None:
                            partition.memory_to_id.pop(memory_id, None)

                await asyncio.to_thread(_remove)
                await asyncio.to_thread(self._save_blocking, partition)

    # --- read path ------------------------------------------------------------ #

    def _search_blocking(
        self,
        partition: _Partition,
        query: list[float],
        top_k: int,
        allowed_ids: set[str] | None,
    ) -> list[VectorHit]:
        import faiss

        total = partition.index.ntotal
        if total == 0:
            return []

        vector = np.asarray([query], dtype=np.float32)
        faiss.normalize_L2(vector)

        # With a pre-filter we must look past the top_k nearest overall, since
        # the eligible rows may sit deeper. Small partitions are scanned whole,
        # which makes the filtered result provably exact.
        if allowed_ids is not None and total <= _EXHAUSTIVE_SCAN_LIMIT:
            search_k = total
        elif allowed_ids is not None:
            search_k = min(total, max(top_k * _OVERFETCH_FACTOR, top_k))
        else:
            search_k = min(total, top_k)

        scores, ids = partition.index.search(vector, search_k)

        hits: list[VectorHit] = []
        for score, faiss_id in zip(scores[0], ids[0], strict=True):
            if faiss_id == -1:  # FAISS pads short result rows with -1.
                continue
            memory_id = partition.id_to_memory.get(int(faiss_id))
            if memory_id is None:
                continue
            if allowed_ids is not None and memory_id not in allowed_ids:
                continue
            hits.append(VectorHit(memory_id=memory_id, score=float(score)))
            if len(hits) >= top_k:
                break
        return hits

    async def search(
        self,
        scope: MemoryScope,
        query_vector: list[float],
        top_k: int = 10,
        allowed_ids: set[str] | None = None,
    ) -> list[VectorHit]:
        if not query_vector or top_k <= 0:
            return []
        # An empty (not None) allow-list means the pre-filter matched nothing;
        # searching would be wasted work and must not fall back to "everything".
        if allowed_ids is not None and not allowed_ids:
            return []

        metrics.increment(VECTOR_SEARCHES, labels={"provider": "faiss"})
        with metrics.timer(VECTOR_SEARCH_LATENCY, {"provider": "faiss"}):
            async with await self._lock_for(self._slug(scope)):
                partition = await self._acquire(scope, len(query_vector))
                if partition.dim != len(query_vector):
                    logger.warning(
                        "faiss_query_dimension_mismatch",
                        partition_dim=partition.dim,
                        query_dim=len(query_vector),
                    )
                    return []
                return await asyncio.to_thread(
                    self._search_blocking, partition, query_vector, top_k, allowed_ids
                )

    async def count(self, scope: MemoryScope) -> int:
        async with await self._lock_for(self._slug(scope)):
            partition = await self._acquire(scope)
            return int(partition.index.ntotal)

    async def clear(self, scope: MemoryScope) -> None:
        slug = self._slug(scope)
        index_path, meta_path, lock_path = self._paths(slug)
        async with await self._lock_for(slug):
            with FileLock(str(lock_path), timeout=30):
                self._loaded.pop(slug, None)
                for path in (index_path, meta_path):
                    if path.is_file():
                        path.unlink()

    async def health_check(self) -> bool:
        try:
            return self._root.is_dir() and os.access(self._root, os.W_OK)
        except OSError:
            return False

    async def destroy_all(self) -> None:
        """Delete every partition on disk. Test helper — never called by the API."""
        self._loaded.clear()
        self._locks.clear()
        if self._root.is_dir():
            shutil.rmtree(self._root)
        self._root.mkdir(parents=True, exist_ok=True)
