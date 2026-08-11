"""Calibrate ER bands against the corpus manifest (docs/plan.md D5:
"thresholds calibrated against the manifest Wed evening"; §15's gate).

Synthesizes the mention set a PERFECT extractor would produce from the
manifest's plantings -- one mention per (document, name planting), with
every same-person element planted in that document attached, trap
plantings (person_uid null, includes staff signatures) omitted per the
D9 hard rule -- then runs the real blocking + feature + scoring +
clustering pure functions and sweeps the band thresholds. Reports, per
candidate auto-link threshold (0.75 / 0.80 / 0.85 / 0.90):

  (a) pairwise precision/recall/F1 over mention pairs,
  (b) SharedName leakage -- cross-person merges within the scripted
      shared-name pairs; MUST be zero (hard constraints guarantee it,
      and the proof section shows every cross-pair's hard_reason),
  (c) NicknameCluster pairwise recall (the planted variant exam),
  (d) gray-band volume = the adjudicator's workload.

Ends with a recommended (auto_link, distinct) pair, compared against
config's (er_auto_link_threshold, er_distinct_threshold) defaults.
Exits 1 if leakage is nonzero at the recommended thresholds.

Run: backend/.venv/bin/python backend/scripts/calibrate_er.py
     [--manifest data/manifest.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.services.er.blocking import generate_candidate_pairs  # noqa: E402
from app.services.er.cluster import cluster_mentions  # noqa: E402
from app.services.er.normalize import ErMention, build_mention  # noqa: E402
from app.services.er.scoring import ScoredPair, score_candidates  # noqa: E402

AUTO_SWEEP = (0.75, 0.80, 0.85, 0.90)
DISTINCT_SWEEP = (0.35, 0.40, 0.45, 0.50)
CONFIG_BANDS = (0.85, 0.45)  # core/config.py er_auto_link/er_distinct defaults


def find_manifest(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    for candidate in (REPO_ROOT / "data/manifest.json", REPO_ROOT / "data/manifest-mini.json"):
        if candidate.exists():
            return candidate
    sys.exit("no manifest found under data/ -- generate the corpus first")


def synthesize_mentions(
    manifest: dict,
) -> tuple[list[ErMention], dict[str, str], Counter]:
    """Perfect-extractor mention set: (mentions, mention_id -> person_uid,
    skip counters). Element-only planting groups (the PartialIdentifiers
    cred dumps -- elements with no name in the doc) produce NO mention:
    they become mention-less pii_elements the DB-wired ER attaches later,
    so they are out of scope for mention-pair calibration."""
    mentions: list[ErMention] = []
    truth: dict[str, str] = {}
    skipped: Counter = Counter()
    for doc in manifest["documents"]:
        if doc.get("problem"):
            skipped["problem_docs"] += 1
            continue
        by_person: dict[str, list[dict]] = defaultdict(list)
        for planting in doc["plantings"]:
            if planting["person_uid"] is None:
                skipped["trap_plantings"] += 1  # incl. staff signatures (D9)
                continue
            by_person[planting["person_uid"]].append(planting)
        for person_uid, plantings in by_person.items():
            names = [p for p in plantings if p["element_type"] == "name"]
            elements = [
                (p["element_type"], p["value"])
                for p in plantings
                if p["element_type"] != "name"
            ]
            if not names:
                skipped["element_only_groups"] += 1
                continue
            for i, name_planting in enumerate(names):
                mention_id = f"{doc['doc_uid']}:{person_uid}:{i}"
                mentions.append(
                    build_mention(
                        mention_id,
                        doc_id=doc["doc_uid"],
                        name_raw=name_planting["value"],
                        elements=elements,
                    )
                )
                truth[mention_id] = person_uid
    return mentions, truth, skipped


def name_kind_index(manifest: dict) -> dict[tuple[str, str], str]:
    """(person_uid, printed name) -> variant kind, for miss diagnostics."""
    index: dict[tuple[str, str], str] = {}
    for identity in manifest["identities"]:
        uid = identity["person_uid"]
        index[(uid, identity["canonical_name"])] = "canonical"
        for variant in identity["name_variants"]:
            index[(uid, variant["value"])] = variant["kind"]
    return index


def true_pairs_of(truth: dict[str, str]) -> set[tuple[str, str]]:
    by_person: dict[str, list[str]] = defaultdict(list)
    for mention_id, uid in truth.items():
        by_person[uid].append(mention_id)
    return {
        tuple(sorted(pair))
        for ids in by_person.values()
        for pair in combinations(ids, 2)
    }


def intra_cluster_pairs(clusters: list[list[str]]) -> set[tuple[str, str]]:
    return {
        tuple(sorted(pair))
        for cluster in clusters
        for pair in combinations(cluster, 2)
    }


def shared_name_leakage(
    clusters: list[list[str]], truth: dict[str, str], scripted_pairs: list[list[str]]
) -> list[tuple[str, str]]:
    leaks = []
    for uid_a, uid_b in scripted_pairs:
        for cluster in clusters:
            uids = {truth[m] for m in cluster}
            if uid_a in uids and uid_b in uids:
                leaks.append((uid_a, uid_b))
                break
    return leaks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", help="manifest path (default: full, else mini)")
    args = parser.parse_args()

    manifest_path = find_manifest(args.manifest)
    manifest = json.loads(manifest_path.read_text())
    print(f"manifest: {manifest_path.relative_to(REPO_ROOT)}")
    print(f"  seed={manifest['seed']} profile={manifest['profile']} "
          f"identities={manifest['generated_counts']['identities']} "
          f"documents={manifest['generated_counts']['documents']}")

    mentions, truth, skipped = synthesize_mentions(manifest)
    kind_index = name_kind_index(manifest)
    mentions_by_id = {m.mention_id: m for m in mentions}
    truth_pairs = true_pairs_of(truth)
    n = len(mentions)
    print(f"\nsynthesized mentions: {n}  (perfect-extractor set)")
    print(f"  skipped: {dict(skipped)}")
    print(f"  true same-person mention pairs: {len(truth_pairs)}")

    # --- blocking ---
    blocking = generate_candidate_pairs(mentions)
    candidate_keys = set(blocking.pairs)
    covered = truth_pairs & candidate_keys
    missed = truth_pairs - candidate_keys
    print(f"\nblocking: {blocking.stats.n_pairs} candidate pairs from "
          f"{blocking.stats.n_blocks} blocks "
          f"(all-pairs would be {n * (n - 1) // 2}; "
          f"ratio {blocking.stats.n_pairs / max(1, n * (n - 1) // 2):.3f})")
    if blocking.stats.oversized_blocks:
        print(f"  oversized blocks skipped: {blocking.stats.oversized_blocks}")
    print(f"  true-pair coverage: {len(covered)}/{len(truth_pairs)} "
          f"= {len(covered) / max(1, len(truth_pairs)):.1%}")
    if missed:
        kinds = Counter()
        for left, right in missed:
            kinds[tuple(sorted((
                kind_index.get((truth[left], mentions_by_id[left].name.raw), "?")
                if mentions_by_id[left].name else "none",
                kind_index.get((truth[right], mentions_by_id[right].name.raw), "?")
                if mentions_by_id[right].name else "none",
            )))] += 1
        print(f"  blocking misses by variant-kind pair: {dict(kinds)}")

    # --- scoring ---
    scored = score_candidates(mentions_by_id, blocking.pairs)
    hard_reasons = Counter(p.hard_reason for p in scored if p.hard_reason)
    print(f"\nscored pairs: {len(scored)}  hard-constraint hits: {dict(hard_reasons)}")

    # --- SharedName hard-constraint proof (threshold-independent) ---
    scripted = manifest["scenario_script"]["shared_name_pairs"]
    nickname_uids = set(manifest["scenario_script"]["nickname_person_uids"])
    cross: list[ScoredPair] = []
    for pair in scored:
        uids = {truth[pair.left_id], truth[pair.right_id]}
        if any(set(s) == uids for s in scripted):
            cross.append(pair)
    print(f"\nSharedName proof: {len(cross)} scored cross-person candidate pairs "
          f"among {len(scripted)} scripted pairs")
    proof = Counter(
        (p.hard_reason or ("below_gray" if p.score <= min(DISTINCT_SWEEP) else "gray"))
        for p in cross
    )
    print(f"  outcomes: {dict(proof)}  "
          f"max cross-pair score: {max((p.score for p in cross), default=0.0):.3f}")

    nickname_truth_pairs = {
        pair for pair in truth_pairs if truth[pair[0]] in nickname_uids
    }

    # --- threshold sweep (clusters depend only on the auto threshold) ---
    print(f"\n{'auto':>5} {'pairP':>7} {'pairR':>7} {'F1':>7} "
          f"{'leak':>5} {'nickR':>7} {'gray@' + format(CONFIG_BANDS[1], '.2f'):>9}")
    per_auto: dict[float, dict] = {}
    for auto in AUTO_SWEEP:
        result = cluster_mentions(
            mentions_by_id, scored, auto_link_threshold=auto,
            distinct_threshold=min(DISTINCT_SWEEP),
        )
        predicted = intra_cluster_pairs(result.clusters)
        tp = len(predicted & truth_pairs)
        precision = tp / len(predicted) if predicted else 1.0
        recall = tp / len(truth_pairs) if truth_pairs else 1.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        leaks = shared_name_leakage(result.clusters, truth, scripted)
        nick_tp = len(predicted & nickname_truth_pairs)
        nick_recall = nick_tp / len(nickname_truth_pairs) if nickname_truth_pairs else 1.0
        gray_at = {
            distinct: sum(
                1
                for p in result.gray_pairs
                if distinct < p.score < auto
            )
            for distinct in DISTINCT_SWEEP
        }
        per_auto[auto] = {
            "precision": precision, "recall": recall, "f1": f1,
            "leaks": leaks, "nick_recall": nick_recall, "gray_at": gray_at,
            "result": result, "predicted": predicted,
        }
        print(f"{auto:>5.2f} {precision:>7.3f} {recall:>7.3f} {f1:>7.3f} "
              f"{len(leaks):>5d} {nick_recall:>7.3f} {gray_at[CONFIG_BANDS[1]]:>9d}")

    # --- distinct sweep: adjudicator workload vs recall ceiling ---
    # Best F1 among leakage-free thresholds; exact ties prefer the config
    # default (a recommendation to change config must be backed by a
    # measured difference, not a tie), then the higher (safer) threshold.
    best_auto = max(
        (a for a in AUTO_SWEEP if not per_auto[a]["leaks"]),
        key=lambda a: (per_auto[a]["f1"], a == CONFIG_BANDS[0], a),
    )
    info = per_auto[best_auto]
    print(f"\ndistinct sweep at auto={best_auto:.2f} "
          f"(gray queue = adjudicator workload; trueGray = queue pairs that are real):")
    print(f"{'distinct':>9} {'gray':>6} {'trueGray':>9} {'lostTrue':>9} {'ceiling':>8}")
    auto_tp = len(info["predicted"] & truth_pairs)
    distinct_rows: dict[float, dict] = {}
    for distinct in DISTINCT_SWEEP:
        queue = [p for p in info["result"].gray_pairs if distinct < p.score < best_auto]
        queue_keys = {tuple(sorted((p.left_id, p.right_id))) for p in queue}
        true_gray = len(queue_keys & truth_pairs)
        # true pairs invisible to both auto-linking and the adjudicator:
        reachable = info["predicted"] | queue_keys
        lost = len(truth_pairs - reachable)
        ceiling = (auto_tp + true_gray) / len(truth_pairs) if truth_pairs else 1.0
        distinct_rows[distinct] = {"gray": len(queue), "true_gray": true_gray,
                                   "lost": lost, "ceiling": ceiling}
        print(f"{distinct:>9.2f} {len(queue):>6d} {true_gray:>9d} {lost:>9d} {ceiling:>8.3f}")

    # Largest distinct threshold that loses no true pairs vs the loosest
    # floor -- same adjudicator recall ceiling, smallest queue. Exact ties
    # (identical lost AND gray) prefer the config default, as above.
    min_lost = distinct_rows[min(DISTINCT_SWEEP)]["lost"]
    lossless = [d for d in DISTINCT_SWEEP if distinct_rows[d]["lost"] == min_lost]
    best_distinct = max(
        lossless,
        key=lambda d: (-distinct_rows[d]["gray"], d == CONFIG_BANDS[1], d),
        default=CONFIG_BANDS[1],
    )

    print(f"\nRECOMMENDATION: auto_link={best_auto:.2f}, distinct={best_distinct:.2f}")
    cfg_auto, cfg_distinct = CONFIG_BANDS
    if (best_auto, best_distinct) == CONFIG_BANDS:
        print(f"  matches config defaults ({cfg_auto:.2f}, {cfg_distinct:.2f}) -- no change needed.")
    else:
        print(f"  DIFFERS from config defaults ({cfg_auto:.2f}, {cfg_distinct:.2f}):")
        print(f"    config:      F1={per_auto[cfg_auto]['f1']:.3f} "
              f"leak={len(per_auto[cfg_auto]['leaks'])} "
              f"gray={distinct_rows.get(cfg_distinct, {}).get('gray', '?')}")
        print(f"    recommended: F1={per_auto[best_auto]['f1']:.3f} "
              f"leak={len(per_auto[best_auto]['leaks'])} "
              f"gray={distinct_rows[best_distinct]['gray']}")

    leaks = per_auto[best_auto]["leaks"]
    if leaks:
        print(f"\nFAIL: SharedName leakage at recommended thresholds: {leaks}")
        sys.exit(1)
    print("\nSharedName leakage at recommended thresholds: 0 (hard constraints held)")


if __name__ == "__main__":
    main()
