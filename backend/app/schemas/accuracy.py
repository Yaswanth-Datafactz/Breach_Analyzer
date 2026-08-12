"""Pydantic response models for /api/v1/accuracy (Handbook §6.2: Pydantic
models on every endpoint). Shapes mirror docs/plan.md §10's methodology
spec exactly -- person P/R/F1, pairwise ER P/R, per-category P/R table,
the wrongly_merged headline, the trap scorecard, and the error-class
histogram are typed fields here, not a bare `dict`, so the design doc's
tables can render straight off this contract the same way costs.py's
typed rows do.

`AccuracyRun` (db/models.py) has no `status` column by design (the task's
assigned schema is fixed) -- `AccuracyRunOut.status` is DERIVED from
started_at/finished_at/metrics (see `_status_for`), mirroring how
`agent_runs.status` is a real column there but not here: the accuracy
table only needed started_at/finished_at/metrics for its purpose (a
git-SHA-stamped, replayable measurement row), so status is a read-time
projection instead of a stored column.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class PersonMetricsOut(BaseModel):
    """§10 outcome taxonomy rollup + person-level P/R/F1 (docs/plan.md
    §10: "Report person-level precision/recall/F1"). Precision/recall use
    the simplest defensible denominators -- see services/accuracy.py's
    `_person_metrics` docstring for the exact formula -- so the numbers
    are reproducible by hand from the counts on this same object."""

    manifest_identities: int
    predicted_persons: int
    matched: int
    missed: int
    split: int
    wrongly_merged: int
    hallucinated: int
    precision: float
    recall: float
    f1: float


class PairwiseErMetricsOut(BaseModel):
    """Standard pairwise entity-resolution evaluation over mentions (§10):
    for every pair of mentions, are they correctly co-clustered per
    identity_links vs the manifest's ground truth of which mentions
    belong to the same person_uid. Scoped to mentions a strong-identifier
    value could resolve back to a manifest identity AND that carry an
    active identity_link -- `mentions_unresolved` / `mentions_unlinked`
    say how much of the corpus fell outside that scope."""

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


class CategoryAccuracyRowOut(BaseModel):
    """One §1 exposure category's confusion counts + P/R, computed over
    MATCHED persons only (docs/plan.md §10)."""

    category: str
    tp: int
    fp: int
    fn: int
    tn: int
    precision: float | None
    recall: float | None


class TrapScorecardOut(BaseModel):
    """The "false-positive traps" headline (docs/plan.md §8/§10): any
    predicted, exposed flag whose evidence traces back to a manifest-
    tagged trap planting, reported separately from ordinary per-category
    FPs -- run-wide, not scoped to matched persons (a trap can leak onto
    a hallucinated or wrongly-merged person too)."""

    total_trap_plantings: int
    trap_derived_fp_flags: int
    trap_derived_fp_evidence_rows: int
    by_category: dict[str, int]
    leak_rate: float | None


class AccuracyMetricsOut(BaseModel):
    person: PersonMetricsOut
    pairwise_er: PairwiseErMetricsOut
    per_category: list[CategoryAccuracyRowOut]
    # Headline numbers pulled out of `person`/`trap_scorecard` so they are
    # never buried in an aggregate (docs/plan.md §10's explicit ask).
    wrongly_merged_manifest_identities: int
    wrongly_merged_predicted_persons: int
    trap_scorecard: TrapScorecardOut
    # error_class -> count, across accuracy_flag_results PLUS the
    # person-level outcomes the fixed schema has no error_class column
    # for (see services/accuracy.py's module docstring "SCHEMA NOTE").
    error_class_histogram: dict[str, int]
    manifest_path: str
    manifest_profile: str | None
    manifest_seed: int | None


class AccuracyRunCreateIn(BaseModel):
    processing_run_id: UUID
    # Repo-root-relative (e.g. "data/manifest-mini.json") or absolute.
    manifest_path: str = "data/manifest.json"
    # Free-text label stamped into config_snapshot (e.g. "economy",
    # "assurance", "measured") -- purely descriptive, not validated
    # against run_accuracy_eval.py's profile enum since a caller scoring
    # an ad-hoc live run may want any label.
    config_profile: str | None = None


def _status_for(started_at: datetime | None, finished_at: datetime | None, metrics: dict | None) -> str:
    if finished_at is not None and metrics is not None:
        return "failed" if "error" in metrics else "finished"
    if started_at is not None:
        return "running"
    return "pending"


class AccuracyRunOut(BaseModel):
    id: UUID
    status: str
    config_snapshot: dict
    metrics: AccuracyMetricsOut | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime

    @classmethod
    def build(cls, run) -> "AccuracyRunOut":
        metrics = run.metrics if (run.metrics and "error" not in run.metrics) else None
        return cls(
            id=run.id,
            status=_status_for(run.started_at, run.finished_at, run.metrics),
            config_snapshot=run.config_snapshot,
            metrics=AccuracyMetricsOut.model_validate(metrics) if metrics else None,
            started_at=run.started_at,
            finished_at=run.finished_at,
            created_at=run.created_at,
        )


class AccuracyRunSummaryOut(BaseModel):
    """List-view row (docs/plan.md §5 GET /accuracy/runs): the headline
    numbers without the full per-category table, so the list stays cheap."""

    id: UUID
    status: str
    config_profile: str | None
    manifest_path: str | None
    processing_run_id: str | None
    person_precision: float | None
    person_recall: float | None
    person_f1: float | None
    wrongly_merged_manifest_identities: int | None
    trap_derived_fp_flags: int | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime

    @classmethod
    def build(cls, run) -> "AccuracyRunSummaryOut":
        metrics = run.metrics if (run.metrics and "error" not in run.metrics) else None
        person = (metrics or {}).get("person") or {}
        trap = (metrics or {}).get("trap_scorecard") or {}
        snapshot = run.config_snapshot or {}
        return cls(
            id=run.id,
            status=_status_for(run.started_at, run.finished_at, run.metrics),
            config_profile=snapshot.get("config_profile"),
            manifest_path=snapshot.get("manifest_path"),
            processing_run_id=snapshot.get("processing_run_id"),
            person_precision=person.get("precision"),
            person_recall=person.get("recall"),
            person_f1=person.get("f1"),
            wrongly_merged_manifest_identities=(metrics or {}).get("wrongly_merged_manifest_identities"),
            trap_derived_fp_flags=trap.get("trap_derived_fp_flags"),
            started_at=run.started_at,
            finished_at=run.finished_at,
            created_at=run.created_at,
        )


class AccuracyRunPageOut(BaseModel):
    items: list[AccuracyRunSummaryOut]
    total: int
    limit: int
    offset: int


class ManifestImportSummaryOut(BaseModel):
    """Not exposed over the API today (import_manifest.py is a CLI-only
    deliverable per the task) -- defined here anyway so the CLI's summary
    print and any future admin endpoint share one shape."""

    manifest_path: str
    identities_upserted: int
    elements_deleted: int
    elements_inserted: int
    overlapping_uid_conflicts: list[str] = Field(default_factory=list)
