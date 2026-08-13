"""Cost/accuracy curve driver (docs/plan.md §9/§10). This module has no
opinion about pass/fail, it only measures -- same posture as services/
accuracy.py and, before it, UC2's own eval scripts.

Two modes:

(a) FRESH RUN, config profile "economy" | "assurance" -- drives the real
    pipeline (services/pipeline.run_processing_run) over a given corpus
    dir with that profile's thresholds applied AND a manifest-driven FAKE
    tier-1/tier-2 adapter standing in for DeepSeek/OpenAI (see
    `ManifestFakeAdapter` below) so Config A/B comparisons are possible
    TODAY, before OPENAI_API_KEY/DEEPSEEK_API_KEY land in backend/.env
    -- zero live LLM calls, a real (non-vacuous) accuracy signal because
    the fake adapter can only "find" a value that is verbatim in the text
    the real parse/OCR pipeline actually produced.

(b) EXISTING RUN, config profile "measured" -- scores an already-
    completed (presumably live-keyed) processing_run_id's persisted
    persons/exposure_flags against the manifest. No pipeline drive, no
    fake adapter, no config override.

Both modes report accuracy (services/accuracy.py) AND cost (cost_events,
via the same repository api/v1/costs.py reads) side by side, stamped with
git SHA + SCHEMA_VERSION/PROMPT_VERSION (accuracy_runs.config_snapshot).

CONFIG APPLICATION (task's explicit either/or): environment-variable
overrides + `get_settings.cache_clear()`, applied IN-PROCESS for the
duration of the eval run and explicitly restored after (`scoped_env_
overrides` below) -- NOT a subprocess-per-config-run. Chosen because mode
(a) needs the SAME fake-adapter object instance visible to services.
pipeline's calls within this one process (a subprocess would need a
different IPC mechanism to inject the fake adapter, unwarranted
complexity for an eval script); documented explicitly per the task's own
instruction not to leave a half-working cache-poisoning bug -- the cache
is always cleared on the way in AND the way out, and env vars unset
before this script ran are actually popped, not left as empty strings.

Run:
    backend/.venv/bin/python backend/scripts/run_accuracy_eval.py \\
        --manifest data/manifest-mini.json --profile economy --corpus data/corpus-mini
    backend/.venv/bin/python backend/scripts/run_accuracy_eval.py \\
        --manifest data/manifest-mini.json --profile assurance --corpus data/corpus-mini
    backend/.venv/bin/python backend/scripts/run_accuracy_eval.py \\
        --manifest data/manifest.json --profile measured --processing-run-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import uuid
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.db.session import SessionLocal  # noqa: E402
from app.repositories.cost_events import CostEventRepository  # noqa: E402
from app.repositories.persons import PersonRepository  # noqa: E402
from app.repositories.runs import ProcessingRunRepository  # noqa: E402
from app.services.accuracy import (  # noqa: E402
    load_manifest,
    non_trap_plantings_by_doc,
    resolve_manifest_path,
    run_accuracy_scoring,
)
from app.services.extraction.base import ExtractionAdapter, ExtractionResult, ExtractionUsage  # noqa: E402
from app.services.extraction.schemas import SCHEMA_VERSION  # noqa: E402

import app.services.pipeline as pipeline_module  # noqa: E402

# ---------------------------------------------------------------------------
# Manifest-driven fake tier-1/tier-2 adapter
# ---------------------------------------------------------------------------

_PASSAGE_RE = re.compile(r"<passage>\n(.*)</passage>", re.DOTALL)


def _passage_text_from_prompt(user_prompt: str) -> str:
    """extraction/prompts.py's `_USER_TEXT_TEMPLATE`/`_REPAIR_TEMPLATE`
    both wrap the chunk text verbatim in `<passage>...</passage>` -- this
    is the one place a fake adapter (which only receives prompt strings,
    never document/chunk objects) can recover exactly what the real
    chunker built (chunker.build_chunks / Chunk.text) for this call."""
    match = _PASSAGE_RE.search(user_prompt)
    return match.group(1) if match else user_prompt


class ManifestFakeAdapter(ExtractionAdapter):
    """Emits exactly the planted NON-TRAP values actually present in each
    chunk of text it is asked to extract from, scoped to whichever
    document is currently "active" (`set_active_document` -- set by
    `drive_fresh_run`'s monkeypatched `_run_extraction_stage` wrapper
    immediately before each document's extraction begins). Never emits a
    trap value -- matching the real system prompt's rule (extraction/
    prompts.py rule 2/3) and the D9 lesson that a correctly-functioning
    system produces zero trap leakage (docs/plan.md §14b measured 0).

    This produces a REAL, non-trivial signal: a value only counts as
    "found" if it is a verbatim substring of the text the real parse/OCR
    pipeline actually produced for that document -- garbled OCR text
    genuinely fails to yield some values, exactly like a real tier-1 call
    would fail on it (extraction/schemas.py's value_raw-must-be-verbatim
    contract).

    VISION MODELING NOTE -- a real, documented simplification, not an
    oversight: `extract_image` first does the same substring match
    `extract_text` would against the (possibly OCR-garbled) chunk text,
    THEN tops up with whatever of the active document's plantings remain
    un-emitted, i.e. it assumes a real vision-capable model reads the
    clean page image rather than the OCR transcription. This is
    optimistic relative to what a real gpt-5.6-terra vision call would
    recover (which could still misread a genuinely bad scan) -- but with
    no live keys available there is no way to simulate actual vision-model
    behavior, and the effect is scoped ONLY to whichever documents reach
    the vision path (Config "economy" never calls extract_image at all,
    so this cannot inflate its numbers -- only "assurance"'s). Restated in
    the printed report, not just here, so it is never missed.
    """

    provider_id = "fake-manifest"
    supports_vision = True

    def __init__(self, manifest: dict):
        self._plantings_by_doc = non_trap_plantings_by_doc(manifest)
        self._active_rel_path: str | None = None
        # rel_path -> {(person_uid, element_type, value_raw)} already
        # emitted by an earlier chunk/call for that document -- so a later
        # chunk (or the vision top-up) never re-emits the same fact twice.
        self._emitted: dict[str, set[tuple[str, str, str]]] = defaultdict(set)

    def set_active_document(self, rel_path: str) -> None:
        self._active_rel_path = rel_path

    def _remaining_groups(self, rel_path: str | None) -> dict[str, list[tuple[str, str]]]:
        if rel_path is None:
            return {}
        groups = self._plantings_by_doc.get(rel_path, {})
        emitted = self._emitted[rel_path]
        remaining: dict[str, list[tuple[str, str]]] = {}
        for person_uid, items in groups.items():
            keep = [(et, v) for et, v in items if (person_uid, et, v) not in emitted]
            if keep:
                remaining[person_uid] = keep
        return remaining

    def _mark_emitted(self, rel_path: str, person_uid: str, items: list[tuple[str, str]]) -> None:
        for element_type, value in items:
            self._emitted[rel_path].add((person_uid, element_type, value))

    @staticmethod
    def _extraction_for_groups(groups: dict[str, list[tuple[str, str]]], *, confidence: float) -> dict:
        mentions: list[dict] = []
        elements: list[dict] = []
        for _person_uid, items in groups.items():
            if not items:
                continue
            name_entry = next((iv for iv in items if iv[0] == "name"), None)
            mention_ref = None
            if name_entry is not None:
                mention_ref = len(mentions)
                mentions.append({"name_raw": name_entry[1], "confidence": confidence})
            for element_type, value in items:
                elements.append(
                    {
                        "element_type": element_type,
                        "value_raw": value,
                        "mention_ref": mention_ref,
                        "confidence": confidence,
                    }
                )
        return {"schema_version": SCHEMA_VERSION, "mentions": mentions, "elements": elements}

    def _extract_from_text(self, text: str) -> ExtractionResult:
        rel_path = self._active_rel_path
        present: dict[str, list[tuple[str, str]]] = {}
        for person_uid, items in self._remaining_groups(rel_path).items():
            found = [(et, v) for et, v in items if v and str(v) in text]
            if not found:
                continue
            has_name_planting = any(element_type == "name" for element_type, _value in items)
            name_found = any(element_type == "name" for element_type, _value in found)
            if has_name_planting and not name_found:
                # This call found SOME of the person's values but not their
                # name (e.g. a scanned page whose OCR preserved an email but
                # garbled the name font) -- emitting them here would give
                # them mention_ref=None (unattached), understating what a
                # real extraction of this SAME text could plausibly do once
                # it ALSO has the image to read the name from (the vision
                # top-up, below). Defer the whole group to a later call
                # instead of fragmenting one person's evidence across an
                # unattached partial hit now and a fresh mention later.
                continue
            present[person_uid] = found
        if rel_path is not None:
            for person_uid, found in present.items():
                self._mark_emitted(rel_path, person_uid, found)
        raw = self._extraction_for_groups(present, confidence=0.92)
        return ExtractionResult(raw_json=raw, usage=ExtractionUsage(0, 0, 0), logprobs=None)

    async def extract_text(self, *, system_prompt: str, user_prompt: str, schema_json: dict) -> ExtractionResult:
        return self._extract_from_text(_passage_text_from_prompt(user_prompt))

    async def extract_image(
        self, *, system_prompt: str, user_prompt: str, image_png_b64: str, schema_json: dict
    ) -> ExtractionResult:
        base = self._extract_from_text(_passage_text_from_prompt(user_prompt))
        rel_path = self._active_rel_path
        remaining = self._remaining_groups(rel_path)
        if rel_path is not None and remaining:
            top_up = self._extraction_for_groups(remaining, confidence=0.85)
            offset = len(base.raw_json["mentions"])
            base.raw_json["mentions"].extend(top_up["mentions"])
            for element in top_up["elements"]:
                if element["mention_ref"] is not None:
                    element = {**element, "mention_ref": element["mention_ref"] + offset}
                base.raw_json["elements"].append(element)
            for person_uid, items in remaining.items():
                self._mark_emitted(rel_path, person_uid, items)
        return base

    async def repair(self, *, system_prompt: str, user_prompt: str, schema_json: dict) -> ExtractionResult:
        # Our own output always validates against PassageExtraction, so
        # this path is not expected to fire -- implemented defensively
        # (same logic) rather than left to raise if it ever does.
        return self._extract_from_text(_passage_text_from_prompt(user_prompt))

    async def complete_json(self, *, system_prompt: str, user_prompt: str) -> ExtractionResult:
        # Not on extraction/service.py's call graph (base.py: "UC2 used
        # this for its decoupled critic") -- present only to satisfy the
        # ABC.
        return ExtractionResult(raw_json={}, usage=ExtractionUsage(0, 0, 0), logprobs=None)


# ---------------------------------------------------------------------------
# Config profiles (docs/plan.md §9's Config A "economy" / Config B "assurance")
# ---------------------------------------------------------------------------

# Env-var names match core/config.py's Settings fields (pydantic-settings
# is case-insensitive by default; uppercase here purely by convention).
CONFIG_PROFILES: dict[str, dict[str, str]] = {
    "economy": {
        "TIER2_ESCALATION_THRESHOLD": "0.6",
        # chunker._is_low_conf_image fires when ocr_mean_conf < threshold;
        # -1 is never satisfied (confidence can't be negative) -- vision
        # escalation OFF, matching §9's Config A.
        "VISION_OCR_CONF_THRESHOLD": "-1",
        "ER_DISTINCT_THRESHOLD": "0.35",
    },
    "assurance": {
        "TIER2_ESCALATION_THRESHOLD": "0.8",
        # 101 > any real 0-100 OCR confidence, so EVERY image-based
        # passage escalates -- vision "on for all image-based passages"
        # per the task text; there is no separate on/off boolean in
        # config.py today, so this is how that qualitative requirement is
        # operationalized onto the one existing threshold knob.
        "VISION_OCR_CONF_THRESHOLD": "101",
        "ER_DISTINCT_THRESHOLD": "0.40",
    },
}

# Required whenever mode (a) drives a fresh run, regardless of profile.
_FAKE_ADAPTER_REQUIRED_OVERRIDES: dict[str, str] = {
    # ManifestFakeAdapter's mutable "active document" context is process-
    # global, not per-task -- correct only when documents are processed
    # one at a time. See drive_fresh_run's docstring.
    "EXTRACTION_MAX_CONCURRENCY": "1",
    # NOT a real credential (never written to backend/.env, never read by
    # anything that makes a network call): services.pipeline.
    # run_processing_run gates the WHOLE extraction stage on
    # bool(settings.deepseek_api_key) alone, live-vs-keyless. get_tier1_
    # adapter/get_tier2_adapter are monkeypatched below to return the fake
    # adapter UNCONDITIONALLY, so this string only flips that boolean
    # gate -- it is never passed to an HTTP client. scoped_env_overrides's
    # `finally` pops it back out before this script exits.
    "DEEPSEEK_API_KEY": "fake-manifest-eval-not-a-real-key",
}


@contextmanager
def scoped_env_overrides(overrides: dict[str, str]):
    """Apply env-var overrides + clear the lru_cached get_settings() for
    the duration of the `with` block; restore both on the way out (see
    module docstring's CONFIG APPLICATION note)."""
    from app.core.config import get_settings

    previous: dict[str, str | None] = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    get_settings.cache_clear()
    try:
        yield get_settings()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Mode (a): drive a fresh pipeline run with the fake adapter
# ---------------------------------------------------------------------------


def drive_fresh_run(corpus_dir: Path, manifest: dict, profile: str) -> uuid.UUID:
    """Runs the REAL pipeline coordinator (services.pipeline.
    run_processing_run -- ingest, parse, tier 0, extraction, and now ER +
    exposure automatically per docs/plan.md §14c's closed gap) with tier-1/
    tier-2 replaced by one shared ManifestFakeAdapter instance.

    The adapter's "active document" context is a plain mutable attribute,
    not thread-local -- correct ONLY when documents are processed strictly
    one at a time, which EXTRACTION_MAX_CONCURRENCY=1 (required override
    above) guarantees: services.pipeline's semaphore(1) means the next
    document's to_thread call cannot start until the current one's
    `_run_extraction_stage` call (and everything inside it) has returned.
    Monkeypatches are attribute swaps on the shared `pipeline_module`
    object, so they are visible from whichever thread the coordinator
    hands work to, regardless of concurrency -- only correctness of the
    ADAPTER'S OWN state depends on strict serialization.

    `_run_extraction_stage` is reached into directly (a private name) for
    the one thing this harness needs that pipeline.py has no public seam
    for: knowing WHICH document is about to be extracted, one call before
    get_tier1_adapter/get_tier2_adapter are invoked with only `settings`
    (no document reference at all). Wrapping it (call our side effect,
    then delegate to the real implementation unchanged) was chosen over
    editing services/pipeline.py, which is outside this task's assigned
    paths.
    """
    processing = ProcessingRunRepository
    db = SessionLocal()
    try:
        config_snapshot = pipeline_module.build_config_snapshot(corpus_path=str(corpus_dir))
        config_snapshot["config_profile"] = profile
        config_snapshot["fake_adapter"] = True
        run = processing(db).create(config_snapshot=config_snapshot)
        db.commit()
        run_id = run.id
    finally:
        db.close()

    adapter = ManifestFakeAdapter(manifest)
    original_stage = pipeline_module._run_extraction_stage

    def _context_setting_stage(db, document):
        adapter.set_active_document(document.rel_path)
        return original_stage(db, document)

    with (
        mock.patch.object(pipeline_module, "get_tier1_adapter", lambda settings: adapter),
        mock.patch.object(pipeline_module, "get_tier2_adapter", lambda settings: adapter),
        mock.patch.object(pipeline_module, "_run_extraction_stage", _context_setting_stage),
    ):
        asyncio.run(pipeline_module.run_processing_run(run_id))

    return run_id


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_report(db, *, accuracy_run, processing_run_id: uuid.UUID, profile: str) -> None:
    metrics = accuracy_run.metrics or {}
    if "error" in metrics:
        print(f"\naccuracy scoring FAILED: {metrics['error']}")
        return

    person = metrics["person"]
    pairwise = metrics["pairwise_er"]
    trap = metrics["trap_scorecard"]
    snapshot = accuracy_run.config_snapshot or {}

    print(f"\n{'=' * 72}\naccuracy report -- profile={profile}\n{'=' * 72}")
    print(f"accuracy_run_id={accuracy_run.id}")
    print(f"processing_run_id={processing_run_id}")
    print(
        f"manifest={snapshot.get('manifest_path')}  git_sha={snapshot.get('git_sha')}  "
        f"schema={snapshot.get('schema_version')}  prompt={snapshot.get('prompt_version')}"
    )

    print("\n-- person-level --")
    print(f"  manifest identities: {person['manifest_identities']}   predicted persons: {person['predicted_persons']}")
    print(
        f"  matched={person['matched']}  missed={person['missed']}  split={person['split']}  "
        f"wrongly_merged={person['wrongly_merged']}  hallucinated={person['hallucinated']}"
    )
    print(f"  precision={person['precision']:.4f}  recall={person['recall']:.4f}  f1={person['f1']:.4f}")
    print(
        f"  WRONGLY_MERGED HEADLINE: {metrics['wrongly_merged_manifest_identities']} manifest identities "
        f"absorbed into {metrics['wrongly_merged_predicted_persons']} predicted person(s)"
    )

    print("\n-- pairwise ER (over mentions) --")
    print(
        f"  scored={pairwise['scored_mentions']}/{pairwise['mentions_total']} "
        f"(ground-truth-unresolved={pairwise['mentions_unresolved']}, unlinked={pairwise['mentions_unlinked']})"
    )
    print(f"  precision={pairwise['precision']:.4f}  recall={pairwise['recall']:.4f}  f1={pairwise['f1']:.4f}")

    print("\n-- per-category (matched persons only) --")
    print(f"  {'category':<20}{'tp':>5}{'fp':>5}{'fn':>5}{'tn':>5}{'precision':>11}{'recall':>9}")
    for row in metrics["per_category"]:
        precision_str = f"{row['precision']:.3f}" if row["precision"] is not None else "n/a"
        recall_str = f"{row['recall']:.3f}" if row["recall"] is not None else "n/a"
        print(
            f"  {row['category']:<20}{row['tp']:>5}{row['fp']:>5}{row['fn']:>5}{row['tn']:>5}"
            f"{precision_str:>11}{recall_str:>9}"
        )

    print("\n-- trap scorecard (run-wide, false-positive traps) --")
    print(
        f"  total trap plantings={trap['total_trap_plantings']}  "
        f"trap-derived FP flags={trap['trap_derived_fp_flags']}  "
        f"evidence rows={trap['trap_derived_fp_evidence_rows']}"
    )
    if trap["by_category"]:
        print(f"  leaked into categories: {trap['by_category']}")

    print("\n-- error class histogram --")
    for error_class, count in sorted(metrics["error_class_histogram"].items()):
        print(f"  {error_class:<20}{count}")

    print("\n-- cost (cost_events, side by side with accuracy) --")
    cost_rows = CostEventRepository(db).summary_for_run(processing_run_id)
    if not cost_rows:
        print("  no cost_events for this processing run.")
    else:
        for row in cost_rows:
            print(f"  {row.purpose:<14}{row.model:<20}calls={row.calls:<6}cost_usd={row.cost_usd:.6f}")
        print(
            f"  TOTAL: calls={sum(r.calls for r in cost_rows)}  "
            f"cost_usd={sum(r.cost_usd for r in cost_rows):.6f}"
        )
    if profile in CONFIG_PROFILES:
        print(
            "  NOTE: manifest-driven FAKE adapter run -- $0.00 / zero real tokens is EXPECTED and "
            "correct (no live LLM calls were made; see this script's module docstring). This run "
            "measures the ACCURACY methodology only, not a real cost estimate -- the live-keyed run "
            "will produce real cost_events per docs/plan.md §14c's readiness checklist."
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True, help="manifest path (repo-root-relative or absolute)")
    parser.add_argument("--profile", required=True, choices=[*sorted(CONFIG_PROFILES), "measured"])
    parser.add_argument("--corpus", help="corpus dir -- required for --profile economy|assurance")
    parser.add_argument(
        "--processing-run-id", help="existing processing run id -- required for --profile measured"
    )
    args = parser.parse_args()

    manifest_file = resolve_manifest_path(args.manifest)
    if not manifest_file.is_file():
        sys.exit(f"manifest not found: {manifest_file}")
    manifest = load_manifest(manifest_file)
    print(
        f"manifest: {args.manifest}  seed={manifest.get('seed')} profile={manifest.get('profile')} "
        f"identities={len(manifest.get('identities', []))} documents={len(manifest.get('documents', []))}"
    )

    if args.profile == "measured":
        if not args.processing_run_id:
            sys.exit("--processing-run-id is required for --profile measured")
        processing_run_id = uuid.UUID(args.processing_run_id)
        db = SessionLocal()
        try:
            if ProcessingRunRepository(db).get(processing_run_id) is None:
                sys.exit(f"processing run {processing_run_id} not found")
            if not PersonRepository(db).iter_for_run(processing_run_id):
                sys.exit(f"processing run {processing_run_id} has no persons/flags yet -- nothing to score")
        finally:
            db.close()
    else:
        if not args.corpus:
            sys.exit(f"--corpus is required for --profile {args.profile}")
        corpus_dir = Path(args.corpus).expanduser().resolve()
        if not corpus_dir.is_dir():
            sys.exit(f"corpus dir not found: {corpus_dir}")
        overrides = {**_FAKE_ADAPTER_REQUIRED_OVERRIDES, **CONFIG_PROFILES[args.profile]}
        print(
            f"\ndriving a FRESH pipeline run over {corpus_dir} with profile={args.profile} "
            f"(manifest-driven fake tier-1/tier-2 adapter -- zero live LLM calls)"
        )
        print(f"  config overrides: { {k: v for k, v in overrides.items() if k != 'DEEPSEEK_API_KEY'} }")
        with scoped_env_overrides(overrides):
            processing_run_id = drive_fresh_run(corpus_dir, manifest, args.profile)
        print(f"  processing_run_id={processing_run_id}")

    db = SessionLocal()
    try:
        accuracy_run = run_accuracy_scoring(
            db,
            processing_run_id=processing_run_id,
            manifest_path=args.manifest,
            manifest=manifest,
            config_profile=args.profile,
        )
        db.commit()
        print_report(db, accuracy_run=accuracy_run, processing_run_id=processing_run_id, profile=args.profile)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
