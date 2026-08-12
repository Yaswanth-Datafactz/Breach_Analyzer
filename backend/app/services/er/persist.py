"""ER database stage (docs/plan.md §3 step 8, D5): load a run's mentions,
run the pure blocking -> features -> scoring -> clustering layer, and
persist the result -- persons, identity_links, er_decisions, and the
gray-band adjudication queue.

Division of labor (unchanged from the pure layer's contract): everything
probabilistic lives in blocking/features/scoring/cluster and is unit-tested
against the manifest; this module only maps rows <-> ErMentions and writes
outcomes. Hard-constraint semantics (enemies never merged, transitively
included) are cluster.py's job and are preserved verbatim -- pairs it
refuses land in er_decisions as no_merge rows, never silently.

Three integration notes that earn their keep:

- employee_id join keys (docs/plan.md §14b B2): employee_id detector hits
  are deliberately NOT pii_elements rows (not a §4 category), so the stage
  re-runs the pure, free tier-0 detectors over each mention's passage text
  and attaches the hits as ErMention elements -- the PartialIdentifiers
  bridge. The keys found are also persisted into mentions.features
  ("employee_id": [...]) so exposure's unattached-element join reads them
  from the row instead of re-OCRing the world.

- Idempotent stage, append-only ledger: re-running the stage wipes and
  rebuilds the run's OWN deterministic output (persons cascade links/flags;
  rule-method er_decisions; open er_pair queue items). The append-only
  discipline of identity_links (D5: unlink = flip active + new row, never
  delete) governs life WITHIN a resolution -- reviewer merges/splits in
  services/review.py -- not the wholesale replay of the stage itself, which
  is the config-snapshot replayability story. Agent/reviewer er_decisions
  and decided review items are never touched by a re-run.

- er_confidence = min auto-link score inside the cluster (the plan's
  weakest-link reading: a person is only as certain as the shakiest link
  holding the cluster together); singletons have no links to doubt -> 1.0.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from rapidfuzz.distance import JaroWinkler
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import Document, Mention, Passage, PiiElement
from app.repositories.er_decisions import ErDecisionRepository
from app.repositories.identity_links import IdentityLinkRepository
from app.repositories.persons import PersonRepository
from app.repositories.review import ReviewRepository
from app.services.detectors import run_tier0
from app.services.detectors import validators as detector_validators
from app.services.er.blocking import generate_candidate_pairs
from app.services.er.cluster import cluster_mentions
from app.services.er.features import SURNAME_TYPO_THRESHOLD
from app.services.er.normalize import NormalizedName, build_mention, normalize_name
from app.services.er.scoring import ScoredPair, score_candidates

logger = get_logger("er.persist")

# Elements usable as ER evidence (validators.py's status vocabulary): for
# verdict-capable types (VERDICT_TYPES: ssn/credit_card/phone/email) only
# 'valid' may pull identities together -- format_only there IS a trap/
# context downgrade and invalid_checksum is a fake, either one merging two
# people is exactly the §10 wrongly_merged failure. For the remaining
# identifier types (financial_account, drivers_license, passport,
# ssn_last4...) no computable check exists, so a plain format_only row
# (trap_reason=None) is the best attainable status and IS usable --
# without this, three of D5's exact-block identifier types (account/DL/
# passport) and the ssn_last4 compatibility feature were dead in the DB
# path while alive in the manifest-calibration path (caught by the B2/B3
# integration pass).


def _usable_element(validation_status: str, element_type: str, signals: dict | None) -> bool:
    if (signals or {}).get("trap_reason"):
        return False
    if element_type in detector_validators.VERDICT_TYPES:
        return validation_status == "valid"
    return validation_status in ("valid", "format_only")

GRAY_REASON = "er_gray_band"


@dataclass
class ErStageResult:
    run_id: uuid.UUID
    mentions: int = 0
    candidate_pairs: int = 0
    scored_pairs: int = 0
    persons: int = 0
    identity_links: int = 0
    er_decisions: int = 0
    gray_items: int = 0
    blocked_by_conflict: int = 0
    hard_conflicts: int = 0
    wiped_persons: int = 0
    # mention_id (str) -> person_id, for callers chaining into exposure
    person_of_mention: dict[str, uuid.UUID] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Load: rows -> ErMentions
# ---------------------------------------------------------------------------


def _employee_id_keys_by_passage(
    db: Session, passage_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """Re-run the pure tier-0 detectors over each passage once and collect
    employee_id hits (trap-downgraded hits excluded). Free, deterministic,
    and the only source of the PartialIdentifiers join key (§14b B2)."""
    if not passage_ids:
        return {}
    keys: dict[uuid.UUID, list[str]] = {}
    rows = db.execute(
        select(Passage.id, Passage.text).where(Passage.id.in_(passage_ids))
    ).all()
    for passage_id, text in rows:
        hits = [
            hit.value_normalized
            for hit in run_tier0(text)
            if hit.element_type == "employee_id" and hit.trap_reason is None
        ]
        if hits:
            keys[passage_id] = sorted(set(hits))
    return keys


def load_er_mentions(db: Session, run_id: uuid.UUID) -> tuple[list, list[Mention]]:
    """Build the pure layer's ErMention list from a run's mentions rows +
    their attached usable elements + employee_id passage keys. Also persists
    the discovered employee_id keys into mentions.features (fresh dict --
    JSONB change tracking) so downstream joins read them from the row."""
    mention_rows = list(
        db.scalars(
            select(Mention)
            .join(Document, Mention.document_id == Document.id)
            .where(Document.run_id == run_id)
            .order_by(Mention.created_at.asc(), Mention.id.asc())
        )
    )
    if not mention_rows:
        return [], []

    mention_ids = [m.id for m in mention_rows]
    element_rows = db.execute(
        select(
            PiiElement.mention_id,
            PiiElement.element_type,
            PiiElement.value_raw,
            PiiElement.validation_status,
            PiiElement.signals,
        ).where(PiiElement.mention_id.in_(mention_ids))
    ).all()
    elements_by_mention: dict[uuid.UUID, list[tuple[str, str]]] = defaultdict(list)
    for mid, element_type, value_raw, validation_status, signals in element_rows:
        if _usable_element(validation_status, element_type, signals):
            elements_by_mention[mid].append((element_type, value_raw))

    passage_keys = _employee_id_keys_by_passage(
        db, sorted({m.passage_id for m in mention_rows})
    )

    er_mentions = []
    for row in mention_rows:
        elements = list(elements_by_mention.get(row.id, []))
        features = dict(row.features or {})
        existing_keys = features.get("employee_id") or []
        if isinstance(existing_keys, str):  # defensive: tier-1 may store one key bare
            existing_keys = [existing_keys]
        keys = sorted({*existing_keys, *passage_keys.get(row.passage_id, [])})
        if keys:
            elements.extend(("employee_id", key) for key in keys)
            if keys != existing_keys:
                features["employee_id"] = keys
                row.features = features  # reassign: JSONB change tracking
        er_mentions.append(
            build_mention(
                str(row.id),
                doc_id=str(row.document_id),
                name_raw=row.name_raw,
                dob_raw=row.dob.isoformat() if row.dob else None,
                elements=elements,
            )
        )
    db.flush()
    return er_mentions, mention_rows


# ---------------------------------------------------------------------------
# Rationale / rule_id derivation (from ScoredPair reasons)
# ---------------------------------------------------------------------------

_CONTRIBUTION_RULE_PARTS = (
    ("name_variant", "name_variant"),
    ("name_similarity", "name"),
    ("dob_match", "dob"),
    ("address_overlap", "address"),
    ("ssn_last4_compatible", "ssn_last4"),
    ("same_doc", "same_doc"),
)


def rule_id_for(pair: ScoredPair) -> str:
    """Compress a ScoredPair's contribution set into the plan's rule_id
    vocabulary (D5 examples: exact_ssn, name_dob_phone)."""
    if pair.hard_reason == "hard_conflict":
        return "hard_conflict"
    contributions = pair.features.get("contributions", {})
    if "shared_strong" in contributions:
        shared = pair.features.get("shared_strong_types") or []
        return "exact_" + "_".join(shared) if shared else "shared_strong"
    parts = [part for key, part in _CONTRIBUTION_RULE_PARTS if key in contributions]
    return "_".join(parts) if parts else "no_evidence"


def rationale_for(pair: ScoredPair) -> str:
    """Human-readable WHY, built from the exact numbers the decision used
    (feature types and weights only -- never identifier values)."""
    contributions = pair.features.get("contributions", {})
    if pair.hard_reason == "hard_conflict":
        types = ", ".join(pair.features.get("conflicting_strong_types", []))
        return f"hard conflict: both mentions carry disjoint {types} values; score forced to 0"
    parts = [f"{name} {value:+.2f}" for name, value in sorted(contributions.items())]
    detail = ", ".join(parts) if parts else "no scoring evidence"
    text = f"score {pair.score:.2f} ({detail})"
    if pair.hard_reason:  # name_only_cap / name_variant_cap
        text += f"; capped: {pair.hard_reason} (uncorroborated name evidence)"
    return text


# ---------------------------------------------------------------------------
# Person shaping (best name, aliases, dob)
# ---------------------------------------------------------------------------


def classify_alias_kind(alias: NormalizedName, best: NormalizedName) -> str:
    """Classify an alias against the chosen best name using the plan's §4
    variant vocabulary (nickname|maiden|initials|misspelling|order_variant).
    Public: services/review.py reuses it when a reviewer merge folds the
    absorbed person's names into the survivor's alias list."""
    if alias.full == best.full:
        return "order_variant" if alias.was_reordered else "misspelling"
    if alias.given_is_initial != best.given_is_initial:
        return "initials"
    if (
        alias.given
        and best.given
        and alias.given != best.given
        and alias.given_canonicals & best.given_canonicals
    ):
        return "nickname"
    if alias.surname and best.surname and alias.surname != best.surname:
        similarity = JaroWinkler.normalized_similarity(alias.surname, best.surname)
        if similarity < SURNAME_TYPO_THRESHOLD:
            return "maiden"
        return "misspelling"
    if alias.was_reordered:
        return "order_variant"
    return "misspelling"


def _shape_person(names_raw: list[str], dobs: list[str]) -> tuple[str, list[dict], str | None]:
    """best_name (most-frequent canonical form; ties prefer fuller then
    alphabetically-first names), aliases with variant kinds, and the
    majority dob."""
    normalized = [(raw, normalize_name(raw)) for raw in names_raw]
    full_counts = Counter(norm.full for _, norm in normalized if norm.full)
    if not full_counts:
        return "(unnamed)", [], None
    best_full = min(
        full_counts,
        key=lambda full: (-full_counts[full], -len(full.split()), full),
    )
    raw_counts = Counter(raw for raw, norm in normalized if norm.full == best_full)
    # Among raws of the winning form prefer the most frequent printing, then
    # the one that needed no reordering, then alphabetical -- deterministic.
    best_raw = min(
        raw_counts,
        key=lambda raw: (-raw_counts[raw], normalize_name(raw).was_reordered, raw),
    )
    best_norm = normalize_name(best_raw)

    aliases: list[dict] = []
    seen: set[str] = set()
    for raw, norm in sorted(normalized, key=lambda item: item[0]):
        if raw == best_raw or raw in seen or not norm.full:
            continue
        seen.add(raw)
        aliases.append({"name": raw, "kind": classify_alias_kind(norm, best_norm)})

    dob_counts = Counter(d for d in dobs if d)
    dob = min(dob_counts, key=lambda d: (-dob_counts[d], d)) if dob_counts else None
    return best_raw, aliases, dob


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def run_er_stage(
    db: Session,
    run_id: uuid.UUID,
    *,
    auto_link_threshold: float | None = None,
    distinct_threshold: float | None = None,
) -> ErStageResult:
    """Load -> block -> score -> cluster -> persist. Commits nothing: the
    caller owns the transaction (API endpoint, script, or test)."""
    settings = get_settings()
    auto = auto_link_threshold if auto_link_threshold is not None else settings.er_auto_link_threshold
    distinct = (
        distinct_threshold if distinct_threshold is not None else settings.er_distinct_threshold
    )

    result = ErStageResult(run_id=run_id)

    er_mentions, mention_rows = load_er_mentions(db, run_id)
    result.mentions = len(er_mentions)
    mention_row_by_id = {str(row.id): row for row in mention_rows}

    # -- wipe this run's own prior deterministic output (idempotent stage) --
    persons_repo = PersonRepository(db)
    result.wiped_persons = persons_repo.delete_for_run(run_id)
    decisions_repo = ErDecisionRepository(db)
    decisions_repo.delete_rule_decisions_for_run(run_id)
    review_repo = ReviewRepository(db)
    review_repo.delete_open_er_items_for_run(run_id)

    if not er_mentions:
        logger.info("er_stage_no_mentions", run_id=str(run_id))
        return result

    mentions_by_id = {m.mention_id: m for m in er_mentions}
    blocking = generate_candidate_pairs(er_mentions)
    result.candidate_pairs = blocking.stats.n_pairs
    scored = score_candidates(mentions_by_id, blocking.pairs)
    result.scored_pairs = len(scored)
    result.hard_conflicts = sum(1 for p in scored if p.hard_reason == "hard_conflict")
    clustering = cluster_mentions(
        list(mentions_by_id),
        scored,
        auto_link_threshold=auto,
        distinct_threshold=distinct,
    )
    result.blocked_by_conflict = len(clustering.blocked_by_conflict)

    # -- persons + identity_links -------------------------------------------
    links_repo = IdentityLinkRepository(db)
    auto_keys = {(p.left_id, p.right_id) for p in clustering.auto_links}
    gray_keys = {(p.left_id, p.right_id) for p in clustering.gray_pairs}
    blocked_keys = {(p.left_id, p.right_id) for p in clustering.blocked_by_conflict}
    gray_endpoints = {m for pair in clustering.gray_pairs for m in (pair.left_id, pair.right_id)}

    # Best auto pair per mention: the link evidence identity_links records.
    best_pair_of: dict[str, ScoredPair] = {}
    for pair in clustering.auto_links:
        for endpoint in (pair.left_id, pair.right_id):
            current = best_pair_of.get(endpoint)
            if current is None or pair.score > current.score:
                best_pair_of[endpoint] = pair

    person_of_root: dict[str, uuid.UUID] = {}
    for cluster in clustering.clusters:
        rows = [mention_row_by_id[mid] for mid in cluster]
        best_name, aliases, dob_iso = _shape_person(
            [row.name_raw for row in rows],
            [mentions_by_id[mid].dob or "" for mid in cluster],
        )
        cluster_pair_scores = [
            p.score
            for p in clustering.auto_links
            if p.left_id in cluster and p.right_id in cluster
        ]
        er_confidence = min(cluster_pair_scores) if cluster_pair_scores else 1.0
        needs_review = any(mid in gray_endpoints for mid in cluster)
        person = persons_repo.create(
            run_id=run_id,
            best_name=best_name,
            aliases=aliases or None,
            dob=date.fromisoformat(dob_iso) if dob_iso else None,
            er_confidence=er_confidence,
            review_status="needs_review" if needs_review else "auto",
            mention_count=len(cluster),
            document_count=len({row.document_id for row in rows}),
        )
        result.persons += 1
        root = clustering.cluster_of[cluster[0]]
        person_of_root[root] = person.id

        for mid in cluster:
            pair = best_pair_of.get(mid)
            if pair is not None:
                links_repo.create(
                    person_id=person.id,
                    mention_id=uuid.UUID(mid),
                    method="rule",
                    score=pair.score,
                    rule_id=rule_id_for(pair),
                    rationale=rationale_for(pair),
                )
            else:
                links_repo.create(
                    person_id=person.id,
                    mention_id=uuid.UUID(mid),
                    method="rule",
                    score=None,
                    rule_id="singleton",
                    rationale="sole mention of this identity; no candidate pair reached the auto-link band",
                )
            result.identity_links += 1
            result.person_of_mention[mid] = person.id

    # -- er_decisions: every scored pair gets a verdict row ------------------
    for pair in scored:
        key = (pair.left_id, pair.right_id)
        same_cluster = clustering.cluster_of.get(pair.left_id) == clustering.cluster_of.get(
            pair.right_id
        )
        if key in blocked_keys:
            decision = "no_merge"
            rationale = (
                "auto-band score refused: union would transitively join mentions with "
                "conflicting strong identifiers (enemy clusters). " + rationale_for(pair)
            )
        elif key in auto_keys:
            decision = "merge"
            rationale = rationale_for(pair)
        elif key in gray_keys:
            decision = "escalate"
            rationale = (
                f"gray band ({distinct:.2f} < score < {auto:.2f}): queued for the "
                "adjudicator. " + rationale_for(pair)
            )
        elif distinct < pair.score < auto and same_cluster:
            decision = "merge"
            rationale = (
                "implied transitively by auto-linked pairs (pair itself scored in the "
                "gray band). " + rationale_for(pair)
            )
        else:
            decision = "no_merge"
            rationale = rationale_for(pair)
        decisions_repo.create(
            left_ref={"mention_id": pair.left_id, "run_id": str(run_id)},
            right_ref={"mention_id": pair.right_id, "run_id": str(run_id)},
            decision=decision,
            method="rule",
            score=pair.score,
            features=pair.features,
            rationale=rationale,
        )
        result.er_decisions += 1

    # -- gray-band pairs -> the adjudicator queue ----------------------------
    for pair in clustering.gray_pairs:
        left_person = result.person_of_mention.get(pair.left_id)
        right_person = result.person_of_mention.get(pair.right_id)
        review_repo.create_item(
            kind="er_pair",
            ref={
                "run_id": str(run_id),
                "left_mention_id": pair.left_id,
                "right_mention_id": pair.right_id,
                "left_person_id": str(left_person) if left_person else None,
                "right_person_id": str(right_person) if right_person else None,
                "score": pair.score,
            },
            reason=GRAY_REASON,
            priority=int(round(pair.score * 100)),
        )
        result.gray_items += 1

    logger.info(
        "er_stage_persisted",
        run_id=str(run_id),
        mentions=result.mentions,
        candidate_pairs=result.candidate_pairs,
        persons=result.persons,
        identity_links=result.identity_links,
        er_decisions=result.er_decisions,
        gray_items=result.gray_items,
        blocked_by_conflict=result.blocked_by_conflict,
        hard_conflicts=result.hard_conflicts,
    )
    return result


def get_gray_pairs(db: Session, run_id: uuid.UUID) -> list:
    """The adjudicator agent's queue (docs/plan.md §3): open er_pair review
    items for this run, highest score (= closest call) first. Each item's
    ref carries both mention ids, both person ids, and the pair score; the
    agent resolves evidence through its own read tools."""
    items, _ = ReviewRepository(db).list_items(
        kind="er_pair", status="open", run_id=run_id, limit=10_000
    )
    return items
