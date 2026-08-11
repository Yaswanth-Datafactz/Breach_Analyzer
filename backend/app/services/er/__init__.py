"""ER pure-function layer (docs/plan.md D5, §6): normalize -> block ->
feature -> score (hard constraints in code) -> cluster into three bands.

Every module here is a pure function over in-memory `ErMention` values --
no DB session, no I/O, no config import -- so the whole layer is unit-
testable against the manifest and replayable byte-for-byte (plan §3's
defensibility argument). Thursday's DB wiring feeds `mentions` rows in and
persists `ScoredPair.features` as the `er_decisions.features` JSONB;
nothing in this package changes when it does.
"""

from __future__ import annotations

from app.services.er.blocking import BlockingResult, generate_candidate_pairs
from app.services.er.cluster import ClusterResult, UnionFind, cluster_mentions
from app.services.er.features import PairFeatures, compute_features
from app.services.er.normalize import (
    ErMention,
    NormalizedName,
    build_mention,
    normalize_name,
    normalize_value,
)
from app.services.er.scoring import ScoredPair, score_candidates, score_pair

__all__ = [
    "BlockingResult",
    "ClusterResult",
    "ErMention",
    "NormalizedName",
    "PairFeatures",
    "ScoredPair",
    "UnionFind",
    "build_mention",
    "cluster_mentions",
    "compute_features",
    "generate_candidate_pairs",
    "normalize_name",
    "normalize_value",
    "score_candidates",
    "score_pair",
]
