"""Incremental clustering over scored pairs (docs/plan.md D5's three
bands): union-find with band thresholds. Pure function over in-memory
mentions -- Thursday's DB wiring maps clusters to `persons` +
`identity_links` rows and gray pairs to the adjudicator queue; nothing
here changes when it does.

Band semantics (thresholds are parameters; config's er_auto_link_threshold
/ er_distinct_threshold are the calibrated defaults):
- score >= auto_link  -> union applied immediately (auto-link band).
- distinct < score < auto_link -> emitted to the gray queue for the
  adjudicator, EXCEPT pairs whose endpoints already share a cluster
  after auto-linking -- adjudicating an already-implied link is pure
  queue noise, and the queue length is literally the adjudicator's
  workload metric calibrate_er.py sweeps.
- score <= distinct -> nothing (distinct band; singletons stay persons).

Hard-conflict propagation is the non-obvious part: a pair scored 0.0 for
'hard_conflict' must not just fail to link -- it must also block a
TRANSITIVE merge (A-B auto, B-C auto would put A and C, who carry
different SSNs, in one cluster). Conflicts are tracked per-root and a
union that would join two conflicting roots is refused; the refused pair
lands in `blocked_by_conflict` for review instead of silently merging
(the §10 wrongly_merged failure this layer exists to prevent).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from app.services.er.scoring import ScoredPair


class UnionFind:
    """Path-compressed, union-by-size disjoint sets over string ids."""

    def __init__(self, ids: Iterable[str] = ()) -> None:
        self._parent: dict[str, str] = {}
        self._size: dict[str, int] = {}
        for item in ids:
            self.add(item)

    def add(self, item: str) -> None:
        if item not in self._parent:
            self._parent[item] = item
            self._size[item] = 1

    def find(self, item: str) -> str:
        self.add(item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:  # path compression
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: str, b: str) -> str:
        """Merge the sets containing a and b; returns the surviving root."""
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b:
            return root_a
        if self._size[root_a] < self._size[root_b]:
            root_a, root_b = root_b, root_a
        self._parent[root_b] = root_a
        self._size[root_a] += self._size[root_b]
        return root_a

    def groups(self) -> list[list[str]]:
        """Deterministic: members sorted within groups, groups sorted by
        first member."""
        by_root: dict[str, list[str]] = {}
        for item in self._parent:
            by_root.setdefault(self.find(item), []).append(item)
        return sorted((sorted(members) for members in by_root.values()))


@dataclass
class ClusterResult:
    clusters: list[list[str]]
    cluster_of: dict[str, str]  # mention_id -> root mention_id
    auto_links: list[ScoredPair] = field(default_factory=list)
    gray_pairs: list[ScoredPair] = field(default_factory=list)  # adjudicator queue
    blocked_by_conflict: list[ScoredPair] = field(default_factory=list)


def cluster_mentions(
    mention_ids: Iterable[str],
    scored_pairs: Sequence[ScoredPair],
    auto_link_threshold: float = 0.85,
    distinct_threshold: float = 0.45,
) -> ClusterResult:
    """Deterministic: auto-band pairs apply in descending score order
    (ties broken by pair ids), so a rerun reproduces the identical
    clustering and the identical gray queue."""
    if auto_link_threshold <= distinct_threshold:
        raise ValueError(
            f"auto_link_threshold ({auto_link_threshold}) must exceed "
            f"distinct_threshold ({distinct_threshold})"
        )

    uf = UnionFind(mention_ids)
    for pair in scored_pairs:
        uf.add(pair.left_id)
        uf.add(pair.right_id)

    # Conflict registry: root -> set of conflicting roots. Rebuilt lazily
    # as unions rename roots (enemy sets merge into the surviving root).
    enemies: dict[str, set[str]] = {}
    for pair in scored_pairs:
        if pair.hard_reason == "hard_conflict":
            root_l, root_r = uf.find(pair.left_id), uf.find(pair.right_id)
            enemies.setdefault(root_l, set()).add(root_r)
            enemies.setdefault(root_r, set()).add(root_l)

    result = ClusterResult(clusters=[], cluster_of={})

    auto_band = sorted(
        (p for p in scored_pairs if p.score >= auto_link_threshold and p.hard_reason is None),
        key=lambda p: (-p.score, p.left_id, p.right_id),
    )
    for pair in auto_band:
        root_l, root_r = uf.find(pair.left_id), uf.find(pair.right_id)
        if root_l == root_r:
            result.auto_links.append(pair)  # already implied transitively
            continue
        if root_r in enemies.get(root_l, ()):  # would join conflicting SSNs
            result.blocked_by_conflict.append(pair)
            continue
        survivor = uf.union(root_l, root_r)
        absorbed = root_r if survivor == root_l else root_l
        # Fold the absorbed root's enemies into the survivor's, and
        # re-point third parties at the surviving root.
        merged = enemies.pop(absorbed, set()) | enemies.pop(survivor, set())
        merged.discard(survivor)
        if merged:
            enemies[survivor] = merged
            for other in merged:
                other_set = enemies.setdefault(other, set())
                other_set.discard(absorbed)
                other_set.add(survivor)
        result.auto_links.append(pair)

    gray_band = sorted(
        (
            p
            for p in scored_pairs
            if distinct_threshold < p.score < auto_link_threshold
        ),
        key=lambda p: (-p.score, p.left_id, p.right_id),
    )
    for pair in gray_band:
        if uf.find(pair.left_id) != uf.find(pair.right_id):
            result.gray_pairs.append(pair)

    result.clusters = uf.groups()
    result.cluster_of = {
        member: cluster[0] for cluster in result.clusters for member in cluster
    }
    return result
