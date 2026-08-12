"""Accuracy harness core (docs/plan.md §10): scores a processing run's
predicted persons/exposure_flags against a manifest's ground truth. This
module has no opinion about pass/fail, it only measures -- same posture
as services/cost.py-adjacent costs.py: every number here is either a
straight aggregate over real rows or a documented, explicit modeling
choice, never an invented one.

Five things worth reading before touching this file:

**MANIFEST SCOPING.** `manifest_identities`/`manifest_elements` (db/
models.py) have no manifest-origin column and `manifest_identities.
person_uid` is GLOBALLY UNIQUE by schema -- verified empirically (see
scripts/import_manifest.py's docstring) that data/manifest.json and
data/manifest-mini.json share the person_uid namespace P0001-P0040 (same
seed, same RNG stream up to the smaller pool) but NOT always the same
underlying identity (the mini profile's generation branches on different
scenario counts partway through). So two manifests CANNOT both be
"loaded" for an overlapping uid at the DB level -- the later import always
wins for that uid, which is inherent to the fixed schema, not a bug here.
This module never trusts "whatever happens to be in manifest_identities"
for scoring: `run_accuracy_scoring`/`execute_accuracy_scoring` always
(re)imports the SPECIFIC manifest being scored against immediately before
scoring (import_manifest_into_db is cheap and idempotent) -- satisfying
the plan's "imported to manifest_identities/manifest_elements" contract
and leaving a durable, ad-hoc-SQL-queryable record of what ground truth
was loaded -- but `score_accuracy` and everything it calls (`load_
manifest_identities`, `_trap_index`, `_mention_ground_truth`, etc.) read
ONLY the manifest dict passed in directly (freshly loaded from the file),
never manifest_identities/manifest_elements. Plan §10's "join flag_
evidence -> pii_elements -> cross-reference manifest_elements OR the
manifest's scenario_tags/trap markers" explicitly offers both; this
module takes the second (the in-memory dict) for every read, specifically
BECAUSE of the scoping hazard above -- a SQL join against manifest_
elements would inherit whatever manifest some OTHER import last wrote for
an overlapping uid, exactly the failure mode this paragraph exists to
avoid. Reading the freshly-loaded dict is correct by construction,
independent of DB state, and is what every test in tests/test_accuracy_
matching.py exercises without touching Postgres at all.

**SCHEMA NOTE on error_class.** docs/plan.md §10 / the task brief describe
error_class as attaching to "every non-TP outcome (missed person, split,
wrongly_merged, flag FP, flag FN)". The ACTUAL db/models.py schema (this
task's fixed contract -- not touched here) only has an `error_class`
column on `accuracy_flag_results`; `accuracy_person_results` has
{manifest_person_uid, matched_person_id, match_basis, outcome} and no
error_class column. Per-row, error_class is therefore only ever persisted
on accuracy_flag_results (fp/fn rows) -- exactly as the schema allows.
Person-level error classes (missed -> missed_extraction|ocr_failure;
split -> er_split; wrongly_merged -> er_overmerge) are still computed and
folded into the RUN-LEVEL `metrics.error_class_histogram` JSONB rollup
(accuracy_runs.metrics), which is where the plan's "error class
histogram" top-level number lives -- just not as a new column on a row
that has none. `hallucinated` has no bucket at all in the plan's 6-class
taxonomy ({missed_extraction, ocr_failure, er_split, er_overmerge,
trap_fp, wrong_category}); rather than force-fitting it into one of
those, the histogram reports it under its own `hallucinated` key so the
gap is visible, not hidden.

**PERSON MATCHING PRECEDENCE.** Stage 1: exact match on a planted strong
identifier (ssn, then financial_account/passport/drivers_license, then
email/phone -- docs/plan.md §10's literal order), verified unique per
manifest across all 160 (full) / 40 (mini) identities for every one of
those six element types (see corpusgen's `validate.py` "no cross-identity
value collisions" invariant; spot-checked again here on both manifests
before writing this module -- zero duplicates in either). Stage 1 also
detects the outcome multiplicities (split / wrongly_merged) since it is
the only stage strong enough to assert them; stage 2 (rapidfuzz name +
DOB, greedy-by-descending-score over the leftover pool -- see
`_fuzzy_match`'s docstring for why greedy instead of scipy's Hungarian
solver) only ever produces a clean 1:1 assignment.

**PAIRWISE ER GROUND TRUTH.** Predicted `mentions` are pipeline artifacts
with no FK back to a manifest planting, so per-mention ground truth is
derived by value-overlap: a mention's linked pii_elements (+ its own
normalized name) are compared against each manifest document's non-trap
planting groups (grouped by person_uid) for that SAME rel_path, and the
best-overlap group wins. This works because plan §8's corpus generator
guarantees no cross-identity value collision and virtually every
mention-bearing document plants a strong identifier alongside the name
(`corpusgen/scenarios.py`'s `_pick_elements`). Mentions with no resolvable
overlap are excluded from the pairwise P/R computation and reported
separately (`mentions_unresolved`) rather than silently dropped.

**PER-FLAG SCOPE.** Per docs/plan.md §10, per-category P/R (`compute_
flag_results`) is computed for MATCHED persons only -- split/wrongly_
merged/hallucinated persons would confound category attribution with the
ER error itself. The trap scorecard is deliberately NOT scoped that way
(`compute_trap_scorecard` is run-wide): a leaked trap can land on any
predicted person, matched or not, and the brief's "false-positive traps"
number needs to catch it wherever it lands.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from rapidfuzz import fuzz
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import (
    EXPOSURE_CATEGORIES,
    AccuracyRun,
    Document,
    ExposureFlag,
    FlagEvidence,
    IdentityLink,
    ManifestElement,
    ManifestIdentity,
    Mention,
    Person,
    PiiElement,
    ProcessingRun,
)
from app.repositories.accuracy import AccuracyRunRepository
from app.repositories.exposure_flags import ExposureFlagRepository
from app.repositories.persons import PersonRepository
from app.repositories.runs import ProcessingRunRepository
from app.services.er.normalize import normalize_value
from app.services.exposure import CATEGORY_BY_ELEMENT_TYPE, _linked_elements_by_person, _usable
from app.services.extraction.prompts import PROMPT_VERSION
from app.services.extraction.schemas import SCHEMA_VERSION

logger = get_logger("accuracy")

_REPO_ROOT = Path(__file__).resolve().parents[3]

# docs/plan.md §10's literal precedence: "SSN full, then account/passport/
# DL, then email/phone". Verified unique per-manifest for all six types
# (module docstring); credit_card is deliberately NOT in this list -- the
# plan's matching precedence never names it, unlike ER's own blocking
# families, and the task calls for following the plan text exactly here.
STRONG_ID_PRECEDENCE: tuple[str, ...] = (
    "ssn",
    "financial_account",
    "passport",
    "drivers_license",
    "email",
    "phone",
)

# Stage 2 fallback (docs/plan.md §10's "remaining predicted persons vs
# remaining manifest identities by rapidfuzz name-similarity + DOB").
# Provisional thresholds in the same spirit as core/config.py's other
# TODO-calibrate constants -- stage 1 is expected to resolve the vast
# majority of the corpus by construction (nearly every planted document
# carries a strong identifier alongside the name), so stage 2's pool is
# small and a greedy assignment over it is not a source of real error.
FUZZY_MIN_SCORE = 0.72
DOB_MATCH_BONUS = 0.15
DOB_CONFLICT_PENALTY = 0.25

# Sentinel prefix for accuracy_person_results.manifest_person_uid when
# outcome='hallucinated' -- the column is NOT NULL but a hallucinated
# predicted person has no manifest counterpart by definition. Chosen to
# be self-describing and never collide with a real "P0001"-style uid.
HALLUCINATED_UID_PREFIX = "__hallucinated__:"

_ALL_PII_ELEMENT_TYPES: tuple[str, ...] = (
    "ssn", "ssn_last4", "dob", "drivers_license", "passport", "financial_account",
    "credit_card", "medical", "credential", "address", "phone", "email", "name",
)


# ---------------------------------------------------------------------------
# Manifest file loading
# ---------------------------------------------------------------------------


def resolve_manifest_path(raw: str) -> Path:
    """Repo-root-relative (e.g. "data/manifest-mini.json") or absolute."""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (_REPO_ROOT / path)


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text())


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return None


# ---------------------------------------------------------------------------
# manifest_identities / manifest_elements import (task 1)
# ---------------------------------------------------------------------------


@dataclass
class ManifestImportSummary:
    manifest_path: str
    identities_upserted: int = 0
    elements_deleted: int = 0
    elements_inserted: int = 0
    # person_uids whose stored attributes changed on THIS import (i.e. a
    # different manifest previously wrote this uid with different
    # values) -- purely informational, surfaced by the CLI so an operator
    # notices cross-manifest uid overlap rather than silently trusting it.
    overlapping_uid_conflicts: list[str] = field(default_factory=list)


def import_manifest_into_db(
    db: Session, manifest: dict, *, manifest_path: str = ""
) -> ManifestImportSummary:
    """Idempotent load into manifest_identities / manifest_elements (db/
    models.py's existing minimal columns -- read, not changed). Scope is
    always THIS manifest's own person_uid set (see module docstring's
    MANIFEST SCOPING note): manifest_identities is upserted by its own
    UNIQUE(person_uid); manifest_elements has no unique constraint at all,
    so it is cleared then reloaded for exactly the uids this manifest
    defines -- never a blanket clear, so a second, disjoint manifest's
    rows (e.g. the full manifest's P0041-P0160 when importing the mini
    manifest's P0001-P0040) are left untouched."""
    summary = ManifestImportSummary(manifest_path=manifest_path)
    identities = manifest.get("identities", [])
    uids = [identity["person_uid"] for identity in identities]

    existing = (
        {
            row.person_uid: row
            for row in db.scalars(
                select(ManifestIdentity).where(ManifestIdentity.person_uid.in_(uids))
            )
        }
        if uids
        else {}
    )

    for identity in identities:
        uid = identity["person_uid"]
        dob_raw = identity.get("dob")
        dob_value = date.fromisoformat(dob_raw) if dob_raw else None
        attributes = {
            "canonical_name": identity["canonical_name"],
            "name_variants": identity.get("name_variants", []),
            "elements": identity.get("elements", {}),
        }
        row = existing.get(uid)
        if row is None:
            db.add(
                ManifestIdentity(
                    person_uid=uid,
                    full_name=identity["canonical_name"],
                    dob=dob_value,
                    attributes=attributes,
                )
            )
        else:
            changed = (
                row.attributes != attributes
                or row.full_name != identity["canonical_name"]
                or row.dob != dob_value
            )
            if changed:
                summary.overlapping_uid_conflicts.append(uid)
            row.full_name = identity["canonical_name"]
            row.dob = dob_value
            row.attributes = attributes
        summary.identities_upserted += 1
    db.flush()

    if uids:
        result = db.execute(delete(ManifestElement).where(ManifestElement.person_uid.in_(uids)))
        summary.elements_deleted = result.rowcount or 0

    for doc in manifest.get("documents", []):
        rel_path = doc["filename"]
        for planting in doc.get("plantings", []):
            uid = planting.get("person_uid")
            if uid is None:
                continue  # trap / staff-signature plantings -- not identity ground truth
            db.add(
                ManifestElement(
                    person_uid=uid,
                    rel_path=rel_path,
                    element_type=planting["element_type"],
                    value=str(planting["value"]),
                    expected_locator=planting.get("location"),
                )
            )
            summary.elements_inserted += 1
    db.flush()
    return summary


# ---------------------------------------------------------------------------
# Manifest ground-truth structures (task 2a)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestIdentityGT:
    person_uid: str
    full_name: str
    dob: date | None
    name_values: frozenset[str]
    strong_ids: dict[str, str]  # element_type -> value_normalized
    expected_categories: frozenset[str]


def _expected_categories_by_uid(manifest: dict) -> dict[str, set[str]]:
    """§10: "categories with at least one non-trap planted element" --
    reuses exposure.py's CATEGORY_BY_ELEMENT_TYPE so a manifest planting
    and a real pii_element map to a category the identical way (trap
    plantings have person_uid=None and are skipped; 'name'/'employee_id'
    map to no category, same as the live pipeline)."""
    out: dict[str, set[str]] = defaultdict(set)
    for doc in manifest.get("documents", []):
        for planting in doc.get("plantings", []):
            uid = planting.get("person_uid")
            if uid is None:
                continue
            category = CATEGORY_BY_ELEMENT_TYPE.get(planting["element_type"])
            if category:
                out[uid].add(category)
    return out


def load_manifest_identities(manifest: dict) -> list[ManifestIdentityGT]:
    categories_by_uid = _expected_categories_by_uid(manifest)
    result = []
    for identity in manifest.get("identities", []):
        elements = identity.get("elements", {})
        names = {identity["canonical_name"]} | {
            v["value"] for v in identity.get("name_variants", [])
        }
        strong_ids = {
            element_type: normalize_value(element_type, elements[element_type])
            for element_type in STRONG_ID_PRECEDENCE
            if elements.get(element_type)
        }
        dob_raw = identity.get("dob")
        result.append(
            ManifestIdentityGT(
                person_uid=identity["person_uid"],
                full_name=identity["canonical_name"],
                dob=date.fromisoformat(dob_raw) if dob_raw else None,
                name_values=frozenset(names),
                strong_ids=strong_ids,
                expected_categories=frozenset(categories_by_uid.get(identity["person_uid"], set())),
            )
        )
    return result


def non_trap_plantings_by_doc(manifest: dict) -> dict[str, dict[str, list[tuple[str, str]]]]:
    """rel_path -> {person_uid: [(element_type, value_raw), ...]}, non-trap
    plantings only. Shared ground-truth index: `_mention_ground_truth`
    below normalizes these itself for value-overlap matching;
    scripts/run_accuracy_eval.py's manifest-driven fake tier adapter uses
    the raw strings directly (it must find the literal text a real LLM
    extraction would see, not a normalized form)."""
    out: dict[str, dict[str, list[tuple[str, str]]]] = defaultdict(lambda: defaultdict(list))
    for doc in manifest.get("documents", []):
        rel_path = doc["filename"]
        for planting in doc.get("plantings", []):
            uid = planting.get("person_uid")
            if uid is None:
                continue
            out[rel_path][uid].append((planting["element_type"], str(planting["value"])))
    return {rel_path: dict(groups) for rel_path, groups in out.items()}


def _trap_forms(raw_value: str) -> frozenset[str]:
    """Every normalized form `raw_value` could take under ANY real
    element_type's normalizer (services/er/normalize.py). A leaked trap
    element's `value_normalized` was computed via `normalize_value(the_
    system's_assigned_type, value_raw)` for SOME type in this same set,
    so membership-testing against the union catches it regardless of
    which category the system mistakenly assigned it."""
    return frozenset(
        normalized
        for element_type in _ALL_PII_ELEMENT_TYPES
        if (normalized := normalize_value(element_type, str(raw_value)))
    )


def _trap_index(manifest: dict) -> dict[str, set[str]]:
    """rel_path -> the set of normalized forms any trap planting at that
    location could have leaked as."""
    idx: dict[str, set[str]] = defaultdict(set)
    for doc in manifest.get("documents", []):
        rel_path = doc["filename"]
        for planting in doc.get("plantings", []):
            if planting.get("person_uid") is not None:
                continue
            idx[rel_path] |= _trap_forms(planting["value"])
    return idx


def _rel_paths_index(manifest: dict) -> tuple[dict[str, set[str]], dict[tuple[str, str], set[str]]]:
    """(uid -> rel_paths of ANY non-trap planting, (uid,category) ->
    rel_paths of a planting mapping to that category) -- the join surface
    error classification uses to ask "was the source document image-
    based in THIS run"."""
    by_uid: dict[str, set[str]] = defaultdict(set)
    by_uid_category: dict[tuple[str, str], set[str]] = defaultdict(set)
    for doc in manifest.get("documents", []):
        rel_path = doc["filename"]
        for planting in doc.get("plantings", []):
            uid = planting.get("person_uid")
            if uid is None:
                continue
            by_uid[uid].add(rel_path)
            category = CATEGORY_BY_ELEMENT_TYPE.get(planting["element_type"])
            if category:
                by_uid_category[(uid, category)].add(rel_path)
    return by_uid, by_uid_category


# ---------------------------------------------------------------------------
# Predicted-side loading (task 2a)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PredictedPersonAgg:
    person_id: uuid.UUID
    best_name: str
    dob: date | None
    strong_ids: dict[str, frozenset[str]]  # element_type -> value_normalized set


def load_predicted_persons(db: Session, run_id: uuid.UUID) -> list[PredictedPersonAgg]:
    """Predicted persons for a run, via its linked mentions' VALID elements
    (docs/plan.md §10's own wording) -- reuses services.exposure's private
    `_linked_elements_by_person`/`_usable` so "valid" means exactly what
    the live exposure computation means (trap-downgraded/invalid-checksum
    elements never count as a strong-identifier match, same as they never
    count toward a real flag)."""
    persons = PersonRepository(db).iter_for_run(run_id)
    elements_by_person = _linked_elements_by_person(db, [p.id for p in persons])
    result = []
    for person in persons:
        strong: dict[str, set[str]] = defaultdict(set)
        for element in elements_by_person.get(person.id, []):
            if element.element_type in STRONG_ID_PRECEDENCE and _usable(element):
                strong[element.element_type].add(element.value_normalized)
        result.append(
            PredictedPersonAgg(
                person_id=person.id,
                best_name=person.best_name,
                dob=person.dob,
                strong_ids={k: frozenset(v) for k, v in strong.items()},
            )
        )
    return result


# ---------------------------------------------------------------------------
# Person matching + outcome taxonomy (task 2a/2b)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonResultRow:
    manifest_person_uid: str
    matched_person_id: uuid.UUID | None
    match_basis: str | None
    outcome: str  # matched|missed|split|wrongly_merged|hallucinated


def _name_similarity(name: str, candidates: frozenset[str]) -> float:
    if not name or not candidates:
        return 0.0
    return max(fuzz.token_sort_ratio(name, candidate) for candidate in candidates) / 100.0


def _fuzzy_match(
    leftover_predicted: list[PredictedPersonAgg],
    leftover_manifest: list[ManifestIdentityGT],
    *,
    min_score: float = FUZZY_MIN_SCORE,
) -> list[tuple[uuid.UUID, str, float, bool]]:
    """Stage 2 (docs/plan.md §10): rapidfuzz name-similarity + DOB over
    whatever stage 1's strong-identifier pass left unmatched. GREEDY
    assignment by descending score, not scipy.optimize.linear_sum_
    assignment: scipy is not already a backend dependency (checked
    requirements.txt), and stage 1 is expected to resolve the large
    majority of the corpus by construction (nearly every planted document
    carries a strong identifier alongside its name -- corpusgen's
    `_pick_elements`), so the residual pool here is small enough that a
    greedy maximum-weight matching is not a meaningful source of error.
    At 100x corpus scale, with a materially larger leftover pool, this is
    the spot to add scipy's Hungarian solver -- documented here rather
    than added speculatively now."""
    candidates: list[tuple[float, uuid.UUID, str, bool]] = []
    for predicted in leftover_predicted:
        for identity in leftover_manifest:
            name_score = _name_similarity(predicted.best_name, identity.name_values)
            dob_match = False
            score = name_score
            if predicted.dob is not None and identity.dob is not None:
                if predicted.dob == identity.dob:
                    score = min(1.0, name_score + DOB_MATCH_BONUS)
                    dob_match = True
                else:
                    score = max(0.0, name_score - DOB_CONFLICT_PENALTY)
            if score >= min_score:
                candidates.append((score, predicted.person_id, identity.person_uid, dob_match))

    candidates.sort(key=lambda c: (-c[0], str(c[1]), c[2]))
    assigned_predicted: set[uuid.UUID] = set()
    assigned_manifest: set[str] = set()
    matches: list[tuple[uuid.UUID, str, float, bool]] = []
    for score, person_id, uid, dob_match in candidates:
        if person_id in assigned_predicted or uid in assigned_manifest:
            continue
        assigned_predicted.add(person_id)
        assigned_manifest.add(uid)
        matches.append((person_id, uid, score, dob_match))
    return matches


def match_persons(
    predicted: list[PredictedPersonAgg],
    manifest_identities: list[ManifestIdentityGT],
) -> list[PersonResultRow]:
    """The full task-2a/2b outcome taxonomy. See module docstring's PERSON
    MATCHING PRECEDENCE note for the two-stage design and why split/
    wrongly_merged can only be asserted from stage 1's relation."""
    manifest_by_uid = {m.person_uid: m for m in manifest_identities}

    # ---- stage 1: strong-identifier exact match, precedence-labeled ----
    pair_basis: dict[tuple[uuid.UUID, str], str] = {}
    for element_type in STRONG_ID_PRECEDENCE:
        value_to_uids: dict[str, set[str]] = defaultdict(set)
        for identity in manifest_identities:
            value = identity.strong_ids.get(element_type)
            if value:
                value_to_uids[value].add(identity.person_uid)
        for person in predicted:
            for value in person.strong_ids.get(element_type, ()):
                for uid in value_to_uids.get(value, ()):
                    pair_basis.setdefault((person.person_id, uid), f"exact_{element_type}")

    by_manifest: dict[str, set[uuid.UUID]] = defaultdict(set)
    by_predicted: dict[uuid.UUID, set[str]] = defaultdict(set)
    for person_id, uid in pair_basis:
        by_manifest[uid].add(person_id)
        by_predicted[person_id].add(uid)

    results: list[PersonResultRow] = []
    resolved_manifest: set[str] = set()
    resolved_predicted: set[uuid.UUID] = set()

    for uid in manifest_by_uid:
        matches = by_manifest.get(uid, set())
        if len(matches) >= 2:
            # This manifest identity's evidence is scattered across
            # multiple predicted persons -- split takes precedence at the
            # per-identity level even if one of those predicted persons
            # ALSO over-merges some other identity (that other identity's
            # own row independently reports wrongly_merged; both are true).
            for person_id in sorted(matches, key=str):
                results.append(
                    PersonResultRow(uid, person_id, pair_basis[(person_id, uid)], "split")
                )
            resolved_manifest.add(uid)
            resolved_predicted |= matches
        elif len(matches) == 1:
            person_id = next(iter(matches))
            resolved_manifest.add(uid)
            resolved_predicted.add(person_id)
            outcome = "wrongly_merged" if len(by_predicted.get(person_id, ())) >= 2 else "matched"
            results.append(
                PersonResultRow(uid, person_id, pair_basis[(person_id, uid)], outcome)
            )
        # else: no stage-1 signal -- left for stage 2 below.

    # ---- stage 2: fuzzy name+dob greedy assignment over the leftovers ----
    leftover_manifest = [
        identity for uid, identity in manifest_by_uid.items() if uid not in resolved_manifest
    ]
    leftover_predicted = [p for p in predicted if p.person_id not in resolved_predicted]
    for person_id, uid, _score, dob_match in _fuzzy_match(leftover_predicted, leftover_manifest):
        resolved_manifest.add(uid)
        resolved_predicted.add(person_id)
        basis = "name_dob_fuzzy" if dob_match else "name_fuzzy"
        results.append(PersonResultRow(uid, person_id, basis, "matched"))

    # ---- leftovers after both stages ----
    for uid in manifest_by_uid:
        if uid not in resolved_manifest:
            results.append(PersonResultRow(uid, None, None, "missed"))
    for person in predicted:
        if person.person_id not in resolved_predicted:
            results.append(
                PersonResultRow(
                    f"{HALLUCINATED_UID_PREFIX}{person.person_id}",
                    person.person_id,
                    None,
                    "hallucinated",
                )
            )

    return results


# ---------------------------------------------------------------------------
# Pairwise entity-resolution evaluation over mentions (task 2b)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairwiseErMetrics:
    mentions_total: int
    mentions_ground_truth_resolved: int
    mentions_unresolved: int
    mentions_with_prediction: int
    mentions_unlinked: int
    scored_mentions: int
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float


def _mention_ground_truth(db: Session, run_id: uuid.UUID, manifest: dict) -> dict[uuid.UUID, str | None]:
    """mention_id -> manifest person_uid, or None if unresolvable (module
    docstring's PAIRWISE ER GROUND TRUTH note)."""
    plantings_by_doc = non_trap_plantings_by_doc(manifest)
    normalized_by_doc: dict[str, dict[str, set[tuple[str, str]]]] = {
        rel_path: {
            uid: {
                (element_type, normalized)
                for element_type, value in items
                if (normalized := normalize_value(element_type, value))
            }
            for uid, items in groups.items()
        }
        for rel_path, groups in plantings_by_doc.items()
    }

    rows = db.execute(
        select(Mention.id, Mention.name_normalized, Document.rel_path)
        .join(Document, Mention.document_id == Document.id)
        .where(Document.run_id == run_id)
    ).all()
    if not rows:
        return {}

    mention_ids = [row[0] for row in rows]
    element_rows = db.execute(
        select(PiiElement.mention_id, PiiElement.element_type, PiiElement.value_normalized).where(
            PiiElement.mention_id.in_(mention_ids)
        )
    ).all()
    elements_by_mention: dict[uuid.UUID, set[tuple[str, str]]] = defaultdict(set)
    for mention_id, element_type, value_normalized in element_rows:
        elements_by_mention[mention_id].add((element_type, value_normalized))

    ground_truth: dict[uuid.UUID, str | None] = {}
    for mention_id, name_normalized, rel_path in rows:
        candidates = normalized_by_doc.get(rel_path, {})
        own = set(elements_by_mention.get(mention_id, set()))
        own.add(("name", name_normalized))
        best_uid, best_score = None, 0
        for uid, values in candidates.items():
            score = len(values & own)
            if score > best_score:
                best_uid, best_score = uid, score
        ground_truth[mention_id] = best_uid if best_score > 0 else None
    return ground_truth


def _predicted_cluster_of_mention(db: Session, run_id: uuid.UUID) -> dict[uuid.UUID, uuid.UUID]:
    rows = db.execute(
        select(IdentityLink.mention_id, IdentityLink.person_id)
        .join(Person, IdentityLink.person_id == Person.id)
        .where(Person.run_id == run_id, IdentityLink.active.is_(True))
    ).all()
    return dict(rows)


def _pairwise_confusion(
    true_of: dict[uuid.UUID, str], pred_of: dict[uuid.UUID, uuid.UUID]
) -> tuple[int, int, int]:
    """(tp, fp, fn) over all mention PAIRS, computed via the standard
    grouped-counting identity (equivalent to, but O(n) instead of O(n^2)
    versus, iterating every pair directly -- see calibrate_er.py for the
    small-n itertools.combinations version this mirrors conceptually):
    TP = sum over predicted clusters of C(same-true-label count, 2);
    (TP+FP) = sum over predicted clusters of C(cluster size, 2);
    (TP+FN) = sum over true clusters of C(cluster size, 2)."""
    ids = sorted(true_of, key=str)
    pred_groups: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for mention_id in ids:
        pred_groups[pred_of[mention_id]].append(mention_id)
    tp = 0
    pred_pairs_total = 0
    for members in pred_groups.values():
        k = len(members)
        pred_pairs_total += k * (k - 1) // 2
        true_counts = Counter(true_of[m] for m in members)
        tp += sum(c * (c - 1) // 2 for c in true_counts.values())

    true_groups: dict[str, list[uuid.UUID]] = defaultdict(list)
    for mention_id in ids:
        true_groups[true_of[mention_id]].append(mention_id)
    true_pairs_total = sum(len(members) * (len(members) - 1) // 2 for members in true_groups.values())

    return tp, pred_pairs_total - tp, true_pairs_total - tp


def _f1(precision: float, recall: float) -> float:
    return (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0


def compute_pairwise_er_metrics(db: Session, run_id: uuid.UUID, manifest: dict) -> PairwiseErMetrics:
    ground_truth = _mention_ground_truth(db, run_id, manifest)
    predicted_cluster = _predicted_cluster_of_mention(db, run_id)

    resolved = {mid: uid for mid, uid in ground_truth.items() if uid is not None}
    scored_ids = sorted(set(resolved) & set(predicted_cluster), key=str)
    true_of = {mid: resolved[mid] for mid in scored_ids}
    pred_of = {mid: predicted_cluster[mid] for mid in scored_ids}
    tp, fp, fn = _pairwise_confusion(true_of, pred_of) if scored_ids else (0, 0, 0)

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    return PairwiseErMetrics(
        mentions_total=len(ground_truth),
        mentions_ground_truth_resolved=len(resolved),
        mentions_unresolved=len(ground_truth) - len(resolved),
        mentions_with_prediction=len(predicted_cluster),
        mentions_unlinked=len(resolved) - len(scored_ids),
        scored_mentions=len(scored_ids),
        tp=tp,
        fp=fp,
        fn=fn,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(_f1(precision, recall), 4),
    )


# ---------------------------------------------------------------------------
# Trap scorecard (task 2d) -- run-wide, not scoped to matched persons
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrapScorecard:
    total_trap_plantings: int
    trap_derived_fp_flags: int
    trap_derived_fp_evidence_rows: int
    by_category: dict[str, int]
    leak_rate: float | None


def compute_trap_scorecard(
    db: Session, run_id: uuid.UUID, manifest: dict, *, trap_index: dict[str, set[str]]
) -> TrapScorecard:
    total_trap_plantings = sum(
        1
        for doc in manifest.get("documents", [])
        for planting in doc.get("plantings", [])
        if planting.get("person_uid") is None
    )
    rows = db.execute(
        select(ExposureFlag.id, ExposureFlag.category, Document.rel_path, PiiElement.value_normalized)
        .join(FlagEvidence, FlagEvidence.exposure_flag_id == ExposureFlag.id)
        .join(PiiElement, FlagEvidence.pii_element_id == PiiElement.id)
        .join(Document, FlagEvidence.document_id == Document.id)
        .where(Document.run_id == run_id, ExposureFlag.exposed.is_(True))
    ).all()

    leaked_flag_ids: set[uuid.UUID] = set()
    leaked_evidence = 0
    by_category: Counter[str] = Counter()
    for flag_id, category, rel_path, value_normalized in rows:
        if value_normalized in trap_index.get(rel_path, ()):
            leaked_flag_ids.add(flag_id)
            leaked_evidence += 1
            by_category[category] += 1

    leak_rate = round(len(leaked_flag_ids) / total_trap_plantings, 4) if total_trap_plantings else None
    return TrapScorecard(
        total_trap_plantings=total_trap_plantings,
        trap_derived_fp_flags=len(leaked_flag_ids),
        trap_derived_fp_evidence_rows=leaked_evidence,
        by_category=dict(by_category),
        leak_rate=leak_rate,
    )


# ---------------------------------------------------------------------------
# Per-flag accuracy + error classification (task 2c/2e) -- matched persons only
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlagResultRow:
    manifest_person_uid: str
    category: str
    expected: bool
    predicted: bool
    outcome: str  # tp|fp|fn|tn
    error_class: str | None


def _flags_by_person(db: Session, person_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, bool]]:
    flags = ExposureFlagRepository(db).flags_for_persons(person_ids)
    out: dict[uuid.UUID, dict[str, bool]] = defaultdict(dict)
    for flag in flags:
        out[flag.person_id][flag.category] = bool(flag.exposed)
    return out


def _evidence_index_for_persons(
    db: Session, person_ids: list[uuid.UUID]
) -> dict[tuple[uuid.UUID, str], list[tuple[str, str]]]:
    if not person_ids:
        return {}
    rows = db.execute(
        select(ExposureFlag.person_id, ExposureFlag.category, Document.rel_path, PiiElement.value_normalized)
        .join(FlagEvidence, FlagEvidence.exposure_flag_id == ExposureFlag.id)
        .join(PiiElement, FlagEvidence.pii_element_id == PiiElement.id)
        .join(Document, FlagEvidence.document_id == Document.id)
        .where(ExposureFlag.person_id.in_(person_ids), ExposureFlag.exposed.is_(True))
    ).all()
    idx: dict[tuple[uuid.UUID, str], list[tuple[str, str]]] = defaultdict(list)
    for person_id, category, rel_path, value_normalized in rows:
        idx[(person_id, category)].append((rel_path, value_normalized))
    return idx


def _documents_by_rel_path(db: Session, run_id: uuid.UUID) -> dict[str, Document]:
    docs = db.scalars(select(Document).where(Document.run_id == run_id)).all()
    return {doc.rel_path: doc for doc in docs}


def _error_class_for_rel_paths(rel_paths: set[str], documents_by_rel_path: dict[str, Document]) -> str:
    """ocr_failure iff EVERY contributing document is image-based in this
    run; missed_extraction otherwise -- including when a contributing
    document never made it into this run at all (still fundamentally an
    extraction-pipeline miss, and the fixed error-class taxonomy has no
    'document never processed' bucket)."""
    docs = [documents_by_rel_path[rp] for rp in rel_paths if rp in documents_by_rel_path]
    if docs and all(bool(doc.is_image_based) for doc in docs):
        return "ocr_failure"
    return "missed_extraction"


def compute_flag_results(
    person_results: list[PersonResultRow],
    manifest_by_uid: dict[str, ManifestIdentityGT],
    *,
    flags_by_person: dict[uuid.UUID, dict[str, bool]],
    evidence_index: dict[tuple[uuid.UUID, str], list[tuple[str, str]]],
    trap_index: dict[str, set[str]],
    rel_paths_by_uid_category: dict[tuple[str, str], set[str]],
    documents_by_rel_path: dict[str, Document],
) -> list[FlagResultRow]:
    rows: list[FlagResultRow] = []
    for result in person_results:
        if result.outcome != "matched":
            continue
        identity = manifest_by_uid[result.manifest_person_uid]
        person_id = result.matched_person_id
        predicted_flags = flags_by_person.get(person_id, {})
        for category in EXPOSURE_CATEGORIES:
            expected = category in identity.expected_categories
            predicted = bool(predicted_flags.get(category))
            error_class: str | None = None
            if expected and predicted:
                outcome = "tp"
            elif predicted and not expected:
                outcome = "fp"
                evidence = evidence_index.get((person_id, category), [])
                is_trap = any(vn in trap_index.get(rp, ()) for rp, vn in evidence)
                error_class = "trap_fp" if is_trap else "wrong_category"
            elif expected and not predicted:
                outcome = "fn"
                rel_paths = rel_paths_by_uid_category.get((result.manifest_person_uid, category), set())
                error_class = _error_class_for_rel_paths(rel_paths, documents_by_rel_path)
            else:
                outcome = "tn"
            rows.append(
                FlagResultRow(result.manifest_person_uid, category, expected, predicted, outcome, error_class)
            )
    return rows


# ---------------------------------------------------------------------------
# Top-level rollup (task 2f)
# ---------------------------------------------------------------------------


@dataclass
class AccuracyScoringResult:
    person_results: list[PersonResultRow]
    flag_results: list[FlagResultRow]
    metrics: dict


def _person_metrics(
    person_results: list[PersonResultRow], manifest_count: int, predicted_count: int
) -> dict:
    """P/R over the simplest defensible denominators (auditable by hand
    from the counts on this same dict): recall = matched / every real
    identity in the manifest; precision = matched / every row this run
    produced in the exposure table (docs/plan.md §1: "Legal needs one
    defensible answer" -- this is literally "of the rows we handed them,
    how many are exactly right" / "of the real people, how many did we
    surface as their own clean row"). Split/wrongly_merged instances
    count against precision AND recall (they are not matched) but are
    reported in full below so a reader who prefers a softer partial-
    credit convention can recompute from the raw counts."""
    matched = sum(1 for r in person_results if r.outcome == "matched")
    missed = sum(1 for r in person_results if r.outcome == "missed")
    split = len({r.manifest_person_uid for r in person_results if r.outcome == "split"})
    wrongly_merged = len(
        {r.manifest_person_uid for r in person_results if r.outcome == "wrongly_merged"}
    )
    hallucinated = sum(1 for r in person_results if r.outcome == "hallucinated")
    precision = (matched / predicted_count) if predicted_count else 1.0
    recall = (matched / manifest_count) if manifest_count else 1.0
    return {
        "manifest_identities": manifest_count,
        "predicted_persons": predicted_count,
        "matched": matched,
        "missed": missed,
        "split": split,
        "wrongly_merged": wrongly_merged,
        "hallucinated": hallucinated,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(_f1(precision, recall), 4),
    }


def _category_table(flag_results: list[FlagResultRow]) -> list[dict]:
    rows = []
    for category in EXPOSURE_CATEGORIES:
        subset = [r for r in flag_results if r.category == category]
        tp = sum(1 for r in subset if r.outcome == "tp")
        fp = sum(1 for r in subset if r.outcome == "fp")
        fn = sum(1 for r in subset if r.outcome == "fn")
        tn = sum(1 for r in subset if r.outcome == "tn")
        rows.append(
            {
                "category": category,
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "tn": tn,
                "precision": round(tp / (tp + fp), 4) if (tp + fp) else None,
                "recall": round(tp / (tp + fn), 4) if (tp + fn) else None,
            }
        )
    return rows


def _error_class_histogram(
    person_results: list[PersonResultRow],
    flag_results: list[FlagResultRow],
    missed_person_error_classes: dict[str, str],
) -> dict[str, int]:
    """Run-level rollup folding in the person-level classes the fixed
    schema has no per-row column for (module docstring's SCHEMA NOTE)."""
    histogram: Counter[str] = Counter()
    for result in person_results:
        if result.outcome == "split":
            histogram["er_split"] += 1
        elif result.outcome == "wrongly_merged":
            histogram["er_overmerge"] += 1
        elif result.outcome == "missed":
            histogram[missed_person_error_classes.get(result.manifest_person_uid, "missed_extraction")] += 1
        elif result.outcome == "hallucinated":
            # Not one of the plan's 6 classes -- reported under its own
            # key rather than force-fit, per the SCHEMA NOTE above.
            histogram["hallucinated"] += 1
    for result in flag_results:
        if result.error_class:
            histogram[result.error_class] += 1
    return dict(histogram)


def score_accuracy(
    db: Session, run_id: uuid.UUID, manifest: dict, *, manifest_path: str = ""
) -> AccuracyScoringResult:
    """The pure(-ish) scoring core: reads the run's predicted persons/
    mentions/elements/flags plus the manifest dict, computes everything,
    and returns it in memory -- no accuracy_* writes here (see
    execute_accuracy_scoring for persistence). Safe to call repeatedly /
    from tests without side effects on the accuracy tables."""
    manifest_identities = load_manifest_identities(manifest)
    manifest_by_uid = {m.person_uid: m for m in manifest_identities}
    predicted = load_predicted_persons(db, run_id)

    person_results = match_persons(predicted, manifest_identities)

    pairwise = compute_pairwise_er_metrics(db, run_id, manifest)
    trap_index = _trap_index(manifest)
    trap_scorecard = compute_trap_scorecard(db, run_id, manifest, trap_index=trap_index)

    rel_paths_by_uid, rel_paths_by_uid_category = _rel_paths_index(manifest)
    documents_by_rel_path = _documents_by_rel_path(db, run_id)

    matched_person_ids = [r.matched_person_id for r in person_results if r.outcome == "matched"]
    flags_by_person = _flags_by_person(db, matched_person_ids)
    evidence_index = _evidence_index_for_persons(db, matched_person_ids)

    flag_results = compute_flag_results(
        person_results,
        manifest_by_uid,
        flags_by_person=flags_by_person,
        evidence_index=evidence_index,
        trap_index=trap_index,
        rel_paths_by_uid_category=rel_paths_by_uid_category,
        documents_by_rel_path=documents_by_rel_path,
    )

    missed_person_error_classes = {
        r.manifest_person_uid: _error_class_for_rel_paths(
            rel_paths_by_uid.get(r.manifest_person_uid, set()), documents_by_rel_path
        )
        for r in person_results
        if r.outcome == "missed"
    }

    wrongly_merged_uids = {r.manifest_person_uid for r in person_results if r.outcome == "wrongly_merged"}
    wrongly_merged_predicted = {
        r.matched_person_id for r in person_results if r.outcome == "wrongly_merged"
    }

    metrics = {
        "person": _person_metrics(person_results, len(manifest_identities), len(predicted)),
        "pairwise_er": asdict(pairwise),
        "per_category": _category_table(flag_results),
        "wrongly_merged_manifest_identities": len(wrongly_merged_uids),
        "wrongly_merged_predicted_persons": len(wrongly_merged_predicted),
        "trap_scorecard": asdict(trap_scorecard),
        "error_class_histogram": _error_class_histogram(
            person_results, flag_results, missed_person_error_classes
        ),
        "manifest_path": manifest_path,
        "manifest_profile": manifest.get("profile"),
        "manifest_seed": manifest.get("seed"),
    }
    return AccuracyScoringResult(person_results=person_results, flag_results=flag_results, metrics=metrics)


# ---------------------------------------------------------------------------
# Orchestration + persistence (tasks 2f/3/4 tie together here)
# ---------------------------------------------------------------------------


def build_accuracy_config_snapshot(
    *,
    processing_run: ProcessingRun,
    manifest_path: str,
    manifest: dict,
    config_profile: str | None,
) -> dict:
    """Top-level accuracy_runs.config_snapshot (task 2f): which processing
    run, which manifest, git SHA, prompt/schema version constants."""
    return {
        "processing_run_id": str(processing_run.id),
        "processing_run_config_snapshot": processing_run.config_snapshot,
        "manifest_path": manifest_path,
        "manifest_seed": manifest.get("seed"),
        "manifest_profile": manifest.get("profile"),
        "manifest_identities": len(manifest.get("identities", [])),
        "manifest_documents": len(manifest.get("documents", [])),
        "git_sha": _git_sha(),
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "config_profile": config_profile,
    }


def create_accuracy_run(
    db: Session,
    *,
    processing_run_id: uuid.UUID,
    manifest_path: str,
    config_profile: str | None = None,
) -> AccuracyRun:
    """Synchronous half of dispatch (mirrors runs.py/agents.py's
    create-row-then-background-task split): a bare row exists immediately
    so a 202 response can carry a real id, and status derives to
    "pending"/"running" until `execute_accuracy_scoring` fills it in."""
    if ProcessingRunRepository(db).get(processing_run_id) is None:
        raise ValueError(f"processing run {processing_run_id} not found")
    accuracy_run = AccuracyRunRepository(db).create(
        config_snapshot={
            "processing_run_id": str(processing_run_id),
            "manifest_path": manifest_path,
            "config_profile": config_profile,
        }
    )
    db.flush()
    return accuracy_run


def execute_accuracy_scoring(
    db: Session, accuracy_run_id: uuid.UUID, *, manifest: dict | None = None
) -> AccuracyRun:
    """Does the real work against an already-created accuracy_runs row:
    (re)imports the manifest, scores, persists person/flag results, and
    ALWAYS leaves the row terminal -- `metrics` holds either the real
    rollup or `{"error": "..."}` (never raises past this function, same
    never-let-a-background-task-crash discipline as services/pipeline.py's
    `_run_er_and_exposure_stage` and api/v1/agents.py's dispatch body)."""
    accuracy_repo = AccuracyRunRepository(db)
    accuracy_run = accuracy_repo.get(accuracy_run_id)
    if accuracy_run is None:
        raise ValueError(f"accuracy run {accuracy_run_id} not found")

    snapshot = accuracy_run.config_snapshot or {}
    processing_run_id = uuid.UUID(snapshot["processing_run_id"])
    manifest_path = snapshot.get("manifest_path", "data/manifest.json")
    config_profile = snapshot.get("config_profile")

    try:
        processing_run = ProcessingRunRepository(db).get(processing_run_id)
        if processing_run is None:
            raise ValueError(f"processing run {processing_run_id} not found")

        if manifest is None:
            manifest = load_manifest(resolve_manifest_path(manifest_path))
        import_summary = import_manifest_into_db(db, manifest, manifest_path=manifest_path)
        db.flush()

        accuracy_run.config_snapshot = build_accuracy_config_snapshot(
            processing_run=processing_run,
            manifest_path=manifest_path,
            manifest=manifest,
            config_profile=config_profile,
        )
        result = score_accuracy(db, processing_run_id, manifest, manifest_path=manifest_path)
    except Exception as exc:  # never let a scoring bug crash the caller
        logger.exception("accuracy_scoring_failed", accuracy_run_id=str(accuracy_run.id))
        accuracy_repo.mark_finished(accuracy_run, metrics={"error": f"{type(exc).__name__}: {exc}"})
        return accuracy_run

    accuracy_repo.bulk_add_person_results(accuracy_run.id, result.person_results)
    accuracy_repo.bulk_add_flag_results(accuracy_run.id, result.flag_results)
    accuracy_repo.mark_finished(accuracy_run, metrics=result.metrics)
    logger.info(
        "accuracy_run_finished",
        accuracy_run_id=str(accuracy_run.id),
        processing_run_id=str(processing_run_id),
        manifest_identities_imported=import_summary.identities_upserted,
        person_precision=result.metrics["person"]["precision"],
        person_recall=result.metrics["person"]["recall"],
        wrongly_merged=result.metrics["wrongly_merged_manifest_identities"],
        trap_derived_fp_flags=result.metrics["trap_scorecard"]["trap_derived_fp_flags"],
    )
    return accuracy_run


def run_accuracy_scoring(
    db: Session,
    *,
    processing_run_id: uuid.UUID,
    manifest_path: str,
    manifest: dict | None = None,
    config_profile: str | None = None,
) -> AccuracyRun:
    """One-shot convenience for synchronous callers (scripts/run_accuracy_
    eval.py): create + execute in a single call. Commits nothing itself
    (matches services/er/persist.py's run_er_stage convention) -- the
    caller owns the transaction."""
    accuracy_run = create_accuracy_run(
        db,
        processing_run_id=processing_run_id,
        manifest_path=manifest_path,
        config_profile=config_profile,
    )
    db.flush()
    return execute_accuracy_scoring(db, accuracy_run.id, manifest=manifest)
