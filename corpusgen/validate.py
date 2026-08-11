"""Corpus validator (docs/plan.md §8): re-opens EVERY generated file with a
real parser (PyMuPDF / python-docx / openpyxl / plain read) and asserts
each planting's value is present at its recorded location; then asserts
scenario quotas, cross-identity value uniqueness (collisions only where
scripted), manifest-count consistency, and prints a per-file_class table.
Non-zero exit on any failure — the generation CLI runs this automatically,
and `python -m corpusgen --validate` runs it standalone.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import pymupdf
from docx import Document
from openpyxl import load_workbook

from corpusgen.config import PROFILES

# Families where a value shared by two identities would corrupt ER ground
# truth. Medical conditions legitimately repeat and are excluded; dob
# repeats are possible and harmless (never a sole identifier).
UNIQUE_FAMILIES = [
    "ssn", "credit_card", "phone", "email", "address", "drivers_license",
    "passport", "financial_account", "username", "password", "employee_id",
]

TRAP_KINDS = [
    "trap_order_number",
    "trap_card_invalid",
    "trap_test_ssn",
    "trap_placeholder",
    "trap_signature_email",
]


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _check_document(doc: dict, corpus_dir: Path, errors: list[str]) -> None:
    path = corpus_dir / doc["filename"]
    uid = doc["doc_uid"]
    if not path.exists():
        errors.append(f"{uid}: file missing: {doc['filename']}")
        return

    file_class = doc["file_class"]
    if file_class == "pdf_digital":
        with pymupdf.open(path) as pdf:
            page_texts = [_collapse(page.get_text()) for page in pdf]
        for p in doc["plantings"]:
            page = p["location"]["page"]
            if page > len(page_texts) or _collapse(p["value"]) not in page_texts[page - 1]:
                errors.append(f"{uid}: {p['element_type']} {p['value']!r} not on page {page}")
    elif file_class == "docx":
        d = Document(str(path))
        for p in doc["plantings"]:
            loc = p["location"]
            if "paragraph" in loc:
                idx = loc["paragraph"]
                ok = idx <= len(d.paragraphs) and p["value"] in d.paragraphs[idx - 1].text
            else:
                try:
                    cell = d.tables[loc["table"] - 1].rows[loc["row"] - 1].cells[loc["col"] - 1]
                    ok = p["value"] in cell.text
                except IndexError:
                    ok = False
            if not ok:
                errors.append(f"{uid}: {p['element_type']} {p['value']!r} not at {loc}")
    elif file_class == "xlsx":
        wb = load_workbook(path, read_only=True)
        try:
            for p in doc["plantings"]:
                loc = p["location"]
                value = wb[loc["sheet"]][loc["cell"]].value
                if value is None or p["value"] not in str(value):
                    errors.append(f"{uid}: {p['element_type']} {p['value']!r} not at {loc}")
        finally:
            wb.close()
    else:  # csv | txt | html — line-oriented plain read
        lines = path.read_text().splitlines()
        for p in doc["plantings"]:
            line_no = p["location"]["line"]
            if line_no > len(lines) or p["value"] not in lines[line_no - 1]:
                errors.append(f"{uid}: {p['element_type']} {p['value']!r} not on line {line_no}")


def _check_scenarios(manifest: dict, errors: list[str]) -> None:
    cfg = PROFILES[manifest["profile"]]
    identities = manifest["identities"]
    documents = manifest["documents"]
    by_uid = {i["person_uid"]: i for i in identities}

    def tagged(tag: str) -> list[dict]:
        return [d for d in documents if tag in d["scenario_tags"]]

    # Every identity appears in at least one planting.
    covered = {
        p["person_uid"] for d in documents for p in d["plantings"] if p["person_uid"]
    }
    for missing in sorted(set(by_uid) - covered):
        errors.append(f"coverage: identity {missing} appears in no document")

    # NicknameCluster: enough persons, 3-5 docs each, ≥3 distinct name forms.
    name_docs: dict[str, list[str]] = defaultdict(list)
    for d in tagged("nickname_cluster"):
        for p in d["plantings"]:
            if p["element_type"] == "name":
                name_docs[p["person_uid"]].append(p["value"])
    if len(name_docs) < cfg.nickname_cluster_persons:
        errors.append(
            f"nickname_cluster: {len(name_docs)} persons < {cfg.nickname_cluster_persons}"
        )
    for uid, names in name_docs.items():
        if not (cfg.nickname_docs_min <= len(names) <= cfg.nickname_docs_max):
            errors.append(f"nickname_cluster: {uid} has {len(names)} docs (want 3-5)")
        if len(set(names)) < 3:
            errors.append(f"nickname_cluster: {uid} uses only {len(set(names))} name forms")

    # SharedName: every canonical-name duplicate is scripted, pairs differ
    # on SSN and DOB, and both members have tagged docs.
    scripted = {frozenset(pair) for pair in manifest["scenario_script"]["shared_name_pairs"]}
    if len(scripted) < cfg.shared_name_pairs:
        errors.append(f"shared_name: {len(scripted)} pairs < {cfg.shared_name_pairs}")
    by_name: dict[str, list[str]] = defaultdict(list)
    for i in identities:
        by_name[i["canonical_name"]].append(i["person_uid"])
    duplicate_groups = {frozenset(uids) for uids in by_name.values() if len(uids) > 1}
    if duplicate_groups != scripted:
        errors.append(
            f"shared_name: duplicate-name groups {duplicate_groups} != scripted {scripted}"
        )
    shared_doc_persons = Counter(
        p["person_uid"]
        for d in tagged("shared_name")
        for p in d["plantings"]
        if p["element_type"] == "name"
    )
    for pair in scripted:
        a, b = tuple(pair)
        if by_uid[a]["elements"]["ssn"] == by_uid[b]["elements"]["ssn"]:
            errors.append(f"shared_name: {a}/{b} share an SSN")
        if by_uid[a]["dob"] == by_uid[b]["dob"]:
            errors.append(f"shared_name: {a}/{b} share a DOB")
        for uid in (a, b):
            if shared_doc_persons[uid] < 2:
                errors.append(f"shared_name: {uid} has <2 tagged docs")

    # PartialIdentifiers: per person, one tagged doc with SSN and no name,
    # one with name (+ last-4) and no full SSN.
    ssn_only: set[str] = set()
    name_only: set[str] = set()
    for d in tagged("partial_identifiers"):
        per_person: dict[str, set[str]] = defaultdict(set)
        for p in d["plantings"]:
            if p["person_uid"]:
                per_person[p["person_uid"]].add(p["element_type"])
        for uid, kinds in per_person.items():
            if "ssn" in kinds and "name" not in kinds:
                ssn_only.add(uid)
            if "name" in kinds and "ssn" not in kinds:
                name_only.add(uid)
    joined = ssn_only & name_only
    if len(joined) < cfg.partial_identifier_persons:
        errors.append(
            f"partial_identifiers: {len(joined)} joinable persons < {cfg.partial_identifier_persons}"
        )

    # BulkSpreadsheet: one xlsx exposing the quota of distinct persons.
    bulk_docs = tagged("bulk_spreadsheet")
    if not any(
        d["file_class"] == "xlsx"
        and len({p["person_uid"] for p in d["plantings"] if p["person_uid"]})
        >= cfg.bulk_spreadsheet_rows
        for d in bulk_docs
    ):
        errors.append(
            f"bulk_spreadsheet: no xlsx doc exposes >= {cfg.bulk_spreadsheet_rows} persons"
        )

    # FalsePositiveTraps: doc quota met, every trap kind present, and no
    # trap value collides with any identity element.
    trap_docs = tagged("false_positive_traps")
    if len(trap_docs) < cfg.trap_docs:
        errors.append(f"false_positive_traps: {len(trap_docs)} docs < {cfg.trap_docs}")
    trap_kinds_seen = {
        p["element_type"] for d in trap_docs for p in d["plantings"]
    }
    for kind in TRAP_KINDS:
        if kind not in trap_kinds_seen:
            errors.append(f"false_positive_traps: kind {kind} never planted")
    identity_values = {
        v for i in identities for v in i["elements"].values()
    }
    for d in documents:
        for p in d["plantings"]:
            if p["element_type"].startswith("trap_") and p["value"] in identity_values:
                errors.append(
                    f"{d['doc_uid']}: trap value {p['value']!r} collides with a real identity"
                )

    # Cross-identity element uniqueness (medical/dob excluded by design).
    for family in UNIQUE_FAMILIES:
        owners: dict[str, list[str]] = defaultdict(list)
        for i in identities:
            owners[i["elements"][family]].append(i["person_uid"])
        for value, uids in owners.items():
            if len(uids) > 1:
                errors.append(f"collision: {family} {value!r} shared by {uids}")

    # Size quotas.
    if len(identities) != cfg.n_identities:
        errors.append(f"identities: {len(identities)} != {cfg.n_identities}")
    if len(documents) < cfg.target_docs:
        errors.append(f"documents: {len(documents)} < {cfg.target_docs}")
    for d in documents:
        if d["problem"] is not None:
            errors.append(f"{d['doc_uid']}: problem should be null (problem files land later)")


def _check_counts(manifest: dict, errors: list[str]) -> None:
    documents = manifest["documents"]
    recomputed = {
        "identities": len(manifest["identities"]),
        "documents": len(documents),
        "plantings": sum(len(d["plantings"]) for d in documents),
        "by_file_class": dict(sorted(Counter(d["file_class"] for d in documents).items())),
        "by_scenario": dict(
            sorted(Counter(t for d in documents for t in d["scenario_tags"]).items())
        ),
    }
    if recomputed != manifest["generated_counts"]:
        errors.append(
            f"generated_counts drift: manifest says {manifest['generated_counts']}, "
            f"recomputed {recomputed}"
        )


def run(manifest_path: Path, corpus_dir: Path) -> int:
    manifest = json.loads(Path(manifest_path).read_text())
    corpus_dir = Path(corpus_dir)
    errors: list[str] = []

    # Corpus dir and manifest must list exactly the same files.
    on_disk = {p.name for p in corpus_dir.iterdir() if p.is_file()}
    listed = {d["filename"] for d in manifest["documents"]}
    for name in sorted(on_disk - listed):
        errors.append(f"unlisted file on disk: {name}")
    for name in sorted(listed - on_disk):
        errors.append(f"manifest file missing on disk: {name}")

    for doc in manifest["documents"]:
        _check_document(doc, corpus_dir, errors)
    _check_scenarios(manifest, errors)
    _check_counts(manifest, errors)

    class_docs = Counter(d["file_class"] for d in manifest["documents"])
    class_plantings: Counter = Counter()
    for d in manifest["documents"]:
        class_plantings[d["file_class"]] += len(d["plantings"])
    print(f"{'file_class':<14}{'docs':>6}{'plantings':>12}")
    for file_class in sorted(class_docs):
        print(f"{file_class:<14}{class_docs[file_class]:>6}{class_plantings[file_class]:>12}")
    print(f"{'TOTAL':<14}{sum(class_docs.values()):>6}{sum(class_plantings.values()):>12}")

    if errors:
        print(f"\nVALIDATION FAILED — {len(errors)} error(s):")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(f"\nValidation passed: {sum(class_docs.values())} documents, "
          f"{sum(class_plantings.values())} plantings verified at their recorded locations.")
    return 0
