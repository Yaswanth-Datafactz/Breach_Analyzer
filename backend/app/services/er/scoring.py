"""Weighted pair scoring with hard constraints IN CODE (docs/plan.md D5:
"never merge on name alone; conflicting strong identifiers block a merge
regardless of score"). The constraints are first-class outputs -- a
`hard_reason` on the ScoredPair -- not silent clamps, so er_decisions
rows and the adjudicator both see WHY a score is what it is.

Hard constraints, in precedence order:
1. hard_conflict  -- any strong identifier type present on both sides
   with disjoint values (two different SSNs, two different employee IDs)
   forces score = 0.0. Applied before, and regardless of, every weight:
   a pair sharing an email but conflicting on SSN still scores 0.0 --
   conservative by design, a wrong split is reviewable, a wrong merge
   poisons the exposure table (§10's wrongly_merged headline metric).
2. name_only_cap  -- name evidence with ZERO corroboration (no shared
   identifier, no DOB match, no last4 bridge, no address overlap, no
   same-doc co-occurrence) is capped below the gray-band floor: pure
   string similarity can never reach the adjudicator, let alone merge.
   This is what makes SharedName leakage structurally zero -- identical
   names with nothing else in common land in the distinct band.
3. name_variant_cap -- a CURATED name-variant hit (nickname map or
   initials) with exact/typo-level surname agreement but still zero
   corroboration is allowed into the gray band -- D5 names the planted
   nickname cases as exactly where enumerable rules run out and the
   adjudicator takes over -- but is capped below every candidate
   auto-link threshold: no variant, however convincing, auto-merges
   without corroborating evidence.

Weights are calibrated by scripts/calibrate_er.py against the manifest
(D5: bands calibrated Wed evening); the constants here are the calibrated
values, and the calibration report is the provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from app.services.er.features import (
    SURNAME_TYPO_THRESHOLD,
    PairFeatures,
    compute_features,
)
from app.services.er.normalize import ErMention

# Contribution weights. A single shared strong identifier alone (0.90)
# clears every candidate auto-link threshold (0.75-0.90 sweep) -- the
# plan's exact_ssn rule expressed as arithmetic; corpus construction
# guarantees no cross-identity strong-value collisions except scripted
# ones, and the scripted ones carry conflicting SSNs/DOBs that rule 1
# catches first.
WEIGHT_ONE_STRONG = 0.90
WEIGHT_TWO_PLUS_STRONG = 0.95
WEIGHT_NAME = 0.35  # x name_similarity (component composite, features.py)
WEIGHT_NICKNAME_OR_INITIALS = 0.25  # curated variant with surname agreement
WEIGHT_DOB_MATCH = 0.20
WEIGHT_ADDRESS_OVERLAP = 0.10  # at >= 0.5 token jaccard
WEIGHT_LAST4_COMPATIBLE = 0.08
WEIGHT_SAME_DOC = 0.04
PENALTY_DOB_CONFLICT = -0.35
PENALTY_LAST4_CONFLICT = -0.30

# Caps (see module docstring). name_only_cap MUST stay below the distinct
# threshold in force (config default 0.45) and name_variant_cap below the
# auto-link threshold (default 0.85) -- calibrate_er.py re-checks both
# whenever it recommends new bands.
DEFAULT_NAME_ONLY_CAP = 0.40
DEFAULT_NAME_VARIANT_CAP = 0.80


@dataclass(frozen=True)
class ScoredPair:
    """The scored candidate pair -- `features` is the JSONB payload
    er_decisions persists (feature values + per-feature contributions +
    the block keys that surfaced the pair)."""

    left_id: str
    right_id: str
    score: float
    features: dict
    hard_reason: str | None


def score_pair(
    left_id: str,
    right_id: str,
    features: PairFeatures,
    blocks: set[str] | None = None,
    name_only_cap: float = DEFAULT_NAME_ONLY_CAP,
    name_variant_cap: float = DEFAULT_NAME_VARIANT_CAP,
) -> ScoredPair:
    payload = features.to_dict()
    if blocks:
        payload["blocks"] = sorted(blocks)

    # Hard constraint 1: conflicting strong identifiers -> forced 0.
    if features.conflicting_strong_types:
        payload["contributions"] = {"hard_conflict": 0.0}
        return ScoredPair(left_id, right_id, 0.0, payload, "hard_conflict")

    contributions: dict[str, float] = {}
    if features.shared_strong_count >= 2:
        contributions["shared_strong"] = WEIGHT_TWO_PLUS_STRONG
    elif features.shared_strong_count == 1:
        contributions["shared_strong"] = WEIGHT_ONE_STRONG
    if features.name_similarity > 0:
        contributions["name_similarity"] = WEIGHT_NAME * features.name_similarity
    curated_variant = (
        (features.nickname_hit or features.initials_compatible)
        and features.surname_similarity >= SURNAME_TYPO_THRESHOLD
    )
    if curated_variant:
        contributions["name_variant"] = WEIGHT_NICKNAME_OR_INITIALS
    if features.dob_match:
        contributions["dob_match"] = WEIGHT_DOB_MATCH
    if features.address_overlap >= 0.5:
        contributions["address_overlap"] = WEIGHT_ADDRESS_OVERLAP
    if features.ssn_last4_compatible:
        contributions["ssn_last4_compatible"] = WEIGHT_LAST4_COMPATIBLE
    if features.same_doc:
        contributions["same_doc"] = WEIGHT_SAME_DOC
    if features.dob_conflict:
        contributions["dob_conflict"] = PENALTY_DOB_CONFLICT
    if features.ssn_last4_conflict:
        contributions["ssn_last4_conflict"] = PENALTY_LAST4_CONFLICT

    score = max(0.0, min(1.0, sum(contributions.values())))
    hard_reason: str | None = None

    # Hard constraints 2/3: name evidence without corroboration. The
    # reason is attached whenever the condition holds -- not only when the
    # numeric cap bites -- so er_decisions always records WHY a name-only
    # pair cannot auto-link, and cluster.py structurally refuses to union
    # any hard_reason pair regardless of where the sweep puts the auto
    # threshold (with today's weights the caps are defense in depth: pure
    # name evidence maxes at 0.35 and a curated variant at ~0.59, both
    # already below their caps).
    if not features.has_corroboration and features.name_similarity > 0:
        if curated_variant:
            hard_reason = "name_variant_cap"
            cap = name_variant_cap
        else:
            hard_reason = "name_only_cap"
            cap = name_only_cap
        if score > cap:
            score = cap
            contributions["cap_applied"] = cap

    payload["contributions"] = {k: round(v, 4) for k, v in contributions.items()}
    return ScoredPair(left_id, right_id, round(score, 4), payload, hard_reason)


def score_candidates(
    mentions_by_id: Mapping[str, ErMention],
    candidate_pairs: Mapping[tuple[str, str], set[str]],
    name_only_cap: float = DEFAULT_NAME_ONLY_CAP,
    name_variant_cap: float = DEFAULT_NAME_VARIANT_CAP,
) -> list[ScoredPair]:
    """Blocking output -> scored pairs, deterministic order (sorted pair
    keys) so a rerun writes byte-identical er_decisions."""
    scored: list[ScoredPair] = []
    for left_id, right_id in sorted(candidate_pairs):
        features = compute_features(mentions_by_id[left_id], mentions_by_id[right_id])
        scored.append(
            score_pair(
                left_id,
                right_id,
                features,
                blocks=candidate_pairs[(left_id, right_id)],
                name_only_cap=name_only_cap,
                name_variant_cap=name_variant_cap,
            )
        )
    return scored
