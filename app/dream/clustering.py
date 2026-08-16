"""Semantic clustering for synthesis.

Agglomerative clustering with average linkage over cosine similarity. Chosen
over k-means because the number of patterns in a user's memory is unknown — it
is precisely what we are trying to discover — and a similarity cut-off expresses
"these belong together" directly, whereas a value of k does not.

Implemented here rather than pulled from scipy/sklearn: the input is at most a
few hundred short vectors, an O(n²·log n) loop finishes in milliseconds, and a
heavyweight scientific dependency in a memory service is not worth the install
size.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.core.logging import get_logger
from app.models.memory import Memory

logger = get_logger(__name__)


@dataclass(slots=True)
class Cluster:
    memories: list[Memory] = field(default_factory=list)
    #: Mean pairwise similarity — how tight the cluster is.
    cohesion: float = 0.0

    @property
    def size(self) -> int:
        return len(self.memories)

    @property
    def memory_ids(self) -> list[str]:
        return [memory.memory_id for memory in self.memories]


def _similarity_matrix(vectors: np.ndarray) -> np.ndarray:
    """Pairwise cosine similarity. Vectors arrive normalised, so this is a dot
    product; normalising again is cheap insurance against a stale index."""
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = vectors / norms
    return unit @ unit.T


def cluster_memories(
    memories: list[Memory],
    *,
    similarity_threshold: float = 0.40,
    min_cluster_size: int = 3,
    max_cluster_size: int = 12,
    max_clusters: int = 20,
) -> list[Cluster]:
    """Group memories into candidate pattern clusters.

    Returns only clusters large enough to constitute evidence, ordered by
    cohesion so the strongest signals are synthesised first when a run is capped.
    """
    usable = [memory for memory in memories if memory.embedding]
    if len(usable) < min_cluster_size:
        return []

    vectors = np.asarray([memory.embedding for memory in usable], dtype=np.float32)
    similarity = _similarity_matrix(vectors)
    count = len(usable)

    # Start with every memory in its own cluster, then repeatedly merge the two
    # closest while they remain above the threshold.
    groups: dict[int, list[int]] = {index: [index] for index in range(count)}

    while len(groups) > 1:
        best_pair: tuple[int, int] | None = None
        best_score = similarity_threshold

        keys = list(groups)
        for i, left in enumerate(keys):
            for right in keys[i + 1 :]:
                if len(groups[left]) + len(groups[right]) > max_cluster_size:
                    continue
                # Average linkage: the mean similarity between every cross pair.
                # Single linkage would chain unrelated memories together through
                # a sequence of weak links.
                block = similarity[np.ix_(groups[left], groups[right])]
                score = float(block.mean())
                if score > best_score:
                    best_score = score
                    best_pair = (left, right)

        if best_pair is None:
            break

        left, right = best_pair
        groups[left].extend(groups.pop(right))

    clusters: list[Cluster] = []
    for indices in groups.values():
        if len(indices) < min_cluster_size:
            continue
        block = similarity[np.ix_(indices, indices)]
        # Mean of the off-diagonal entries: the diagonal is all 1.0 and would
        # flatter a loose cluster.
        pair_count = len(indices) * (len(indices) - 1)
        cohesion = float((block.sum() - len(indices)) / pair_count) if pair_count else 0.0
        clusters.append(
            Cluster(memories=[usable[index] for index in indices], cohesion=cohesion)
        )

    clusters.sort(key=lambda cluster: (cluster.cohesion, cluster.size), reverse=True)

    logger.info(
        "memories_clustered",
        input_memories=len(usable),
        clusters=len(clusters),
        returned=min(len(clusters), max_clusters),
    )
    return clusters[:max_clusters]
