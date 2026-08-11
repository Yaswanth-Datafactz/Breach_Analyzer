"""Manifest writer (docs/plan.md §8): the ground-truth answer key.

Schema: {seed, profile, generated_counts, scenario_script, identities,
documents}. `scenario_script` records the deliberately scripted collisions
(shared-name pairs) so validate.py can assert that EVERY canonical-name
duplicate in the pool is scripted, never accidental. Pretty-printed JSON,
byte-stable for a given seed.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from corpusgen.config import CorpusConfig
from corpusgen.identities import Identity, IdentityPool


@dataclass
class ManifestDocument:
    doc_uid: str
    filename: str
    file_class: str
    renderer: str
    scenario_tags: list[str]
    problem: dict | None  # always None today; problem-file scenarios fill this
    plantings: list[dict]


def identity_dict(identity: Identity) -> dict:
    return {
        "person_uid": identity.person_uid,
        "canonical_name": identity.canonical_name,
        "name_variants": [{"value": v.value, "kind": v.kind} for v in identity.variants],
        "dob": identity.dob,
        "elements": dict(identity.elements),
    }


def document_dict(doc: ManifestDocument) -> dict:
    return {
        "doc_uid": doc.doc_uid,
        "filename": doc.filename,
        "file_class": doc.file_class,
        "renderer": doc.renderer,
        "scenario_tags": doc.scenario_tags,
        "problem": doc.problem,
        "plantings": doc.plantings,
    }


def build(
    seed: int,
    cfg: CorpusConfig,
    pool: IdentityPool,
    documents: list[ManifestDocument],
) -> dict:
    by_class = Counter(d.file_class for d in documents)
    by_scenario = Counter(tag for d in documents for tag in d.scenario_tags)
    return {
        "seed": seed,
        "profile": cfg.profile,
        "generated_counts": {
            "identities": len(pool.identities),
            "documents": len(documents),
            "plantings": sum(len(d.plantings) for d in documents),
            "by_file_class": dict(sorted(by_class.items())),
            "by_scenario": dict(sorted(by_scenario.items())),
        },
        "scenario_script": {
            "shared_name_pairs": [list(pair) for pair in pool.shared_name_pairs],
            "nickname_person_uids": list(pool.nickname_person_uids),
        },
        "identities": [identity_dict(i) for i in pool.identities],
        "documents": [document_dict(d) for d in documents],
    }


def write(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")


def load(path: Path) -> dict:
    return json.loads(path.read_text())
