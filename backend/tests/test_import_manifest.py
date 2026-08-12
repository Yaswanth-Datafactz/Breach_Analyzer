"""DB-backed tests for services/accuracy.py's import_manifest_into_db --
idempotence and the "never touches another manifest's rows" scoping
(docs/plan.md §4's fixed manifest_identities/manifest_elements schema;
task 5's "import_manifest idempotence"). Runs against the real Postgres
on :5434 (UC2's DB-test convention -- see tests/test_costs_api.py),
using clearly test-scoped person_uids so nothing here can collide with
or corrupt a real corpusgen-generated manifest's rows; every test cleans
up its own uids in a `finally` block regardless of outcome.
"""

from __future__ import annotations

import uuid as uuid_lib

import pytest
from sqlalchemy import delete, select

from app.db.models import ManifestElement, ManifestIdentity
from app.db.session import SessionLocal
from app.services.accuracy import import_manifest_into_db


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _uid(label: str) -> str:
    return f"TEST-{label}-{uuid_lib.uuid4().hex[:8]}"


def _cleanup(db, uids: list[str]) -> None:
    db.rollback()
    db.execute(delete(ManifestElement).where(ManifestElement.person_uid.in_(uids)))
    db.execute(delete(ManifestIdentity).where(ManifestIdentity.person_uid.in_(uids)))
    db.commit()


def _manifest(identities: list[dict], documents: list[dict]) -> dict:
    return {"seed": 1, "profile": "test", "identities": identities, "documents": documents}


def _identity(uid: str, name: str, dob: str = "1990-01-01", **elements: str) -> dict:
    return {"person_uid": uid, "canonical_name": name, "dob": dob, "name_variants": [], "elements": elements}


def _doc(filename: str, plantings: list[dict]) -> dict:
    return {"filename": filename, "plantings": plantings}


def _planting(uid: str | None, element_type: str, value: str) -> dict:
    return {"person_uid": uid, "element_type": element_type, "value": value}


def test_import_upserts_identities_and_loads_elements(db):
    uid = _uid("BASIC")
    manifest = _manifest(
        [_identity(uid, "Basic Person", ssn="111-22-3333")],
        [_doc("D_basic.pdf", [_planting(uid, "name", "Basic Person"), _planting(uid, "ssn", "111-22-3333")])],
    )
    try:
        summary = import_manifest_into_db(db, manifest, manifest_path="test-basic.json")
        db.commit()

        assert summary.identities_upserted == 1
        assert summary.elements_inserted == 2
        assert summary.overlapping_uid_conflicts == []

        identity = db.scalar(select(ManifestIdentity).where(ManifestIdentity.person_uid == uid))
        assert identity is not None
        assert identity.full_name == "Basic Person"
        assert identity.attributes["elements"]["ssn"] == "111-22-3333"

        elements = db.scalars(select(ManifestElement).where(ManifestElement.person_uid == uid)).all()
        assert {(e.element_type, e.value) for e in elements} == {("name", "Basic Person"), ("ssn", "111-22-3333")}
    finally:
        _cleanup(db, [uid])


def test_import_skips_trap_plantings(db):
    uid = _uid("TRAPSKIP")
    manifest = _manifest(
        [_identity(uid, "Trap Skip Person", ssn="222-33-4444")],
        [
            _doc(
                "D_trapskip.pdf",
                [
                    _planting(uid, "name", "Trap Skip Person"),
                    _planting(uid, "ssn", "222-33-4444"),
                    _planting(None, "trap_order_number", "384-05-3312"),
                ],
            )
        ],
    )
    try:
        summary = import_manifest_into_db(db, manifest, manifest_path="test-trapskip.json")
        db.commit()
        assert summary.elements_inserted == 2  # trap planting (person_uid=None) never becomes a row
        elements = db.scalars(select(ManifestElement).where(ManifestElement.person_uid == uid)).all()
        assert len(elements) == 2
    finally:
        _cleanup(db, [uid])


def test_import_is_idempotent(db):
    """Re-running the SAME manifest import must not duplicate rows and
    must not report a conflict against itself (task 5's explicit ask)."""
    uid = _uid("IDEMP")
    manifest = _manifest(
        [_identity(uid, "Idem Potent", ssn="333-44-5555")],
        [_doc("D_idemp.pdf", [_planting(uid, "ssn", "333-44-5555"), _planting(uid, "name", "Idem Potent")])],
    )
    try:
        first = import_manifest_into_db(db, manifest, manifest_path="idemp.json")
        db.commit()
        second = import_manifest_into_db(db, manifest, manifest_path="idemp.json")
        db.commit()

        assert first.identities_upserted == second.identities_upserted == 1
        assert first.overlapping_uid_conflicts == []
        assert second.overlapping_uid_conflicts == []  # identical content re-imported -> no conflict

        identities = db.scalars(select(ManifestIdentity).where(ManifestIdentity.person_uid == uid)).all()
        assert len(identities) == 1  # upsert, never a duplicate row

        elements = db.scalars(select(ManifestElement).where(ManifestElement.person_uid == uid)).all()
        assert len(elements) == 2  # cleared-then-reloaded, not accumulated across the two imports
    finally:
        _cleanup(db, [uid])


def test_import_scoped_to_manifest_uids_never_touches_other_manifests_rows(db):
    """Two "manifests" sharing one uid (the real data/manifest.json vs
    data/manifest-mini.json situation -- see services/accuracy.py's
    MANIFEST SCOPING docstring) plus each having a uid the other does not
    define. Importing the second must: overwrite the shared uid (an
    inherent property of the fixed global-UNIQUE(person_uid) schema, and
    reported as a conflict), leave the first manifest's EXCLUSIVE uid
    completely untouched, and add the second's exclusive uid fresh."""
    uid_shared = _uid("SHARED")
    uid_a_only = _uid("AONLY")
    uid_b_only = _uid("BONLY")
    try:
        manifest_a = _manifest(
            [
                _identity(uid_shared, "Shared Original", ssn="100000001"),
                _identity(uid_a_only, "A Only Person", ssn="200000002"),
            ],
            [
                _doc("DA1.pdf", [_planting(uid_shared, "ssn", "100000001"), _planting(uid_shared, "name", "Shared Original")]),
                _doc("DA2.pdf", [_planting(uid_a_only, "ssn", "200000002"), _planting(uid_a_only, "name", "A Only Person")]),
            ],
        )
        import_manifest_into_db(db, manifest_a, manifest_path="A.json")
        db.commit()

        manifest_b = _manifest(
            [
                _identity(uid_shared, "Shared Renamed", ssn="999999999"),  # different attributes, same uid
                _identity(uid_b_only, "B Only Person", ssn="300000003"),
            ],
            [
                _doc("DB1.pdf", [_planting(uid_shared, "ssn", "999999999"), _planting(uid_shared, "name", "Shared Renamed")]),
                _doc("DB2.pdf", [_planting(uid_b_only, "ssn", "300000003"), _planting(uid_b_only, "name", "B Only Person")]),
            ],
        )
        summary_b = import_manifest_into_db(db, manifest_b, manifest_path="B.json")
        db.commit()

        # uid_shared: B's version wins -- flagged as a conflict, not silent.
        shared_row = db.scalar(select(ManifestIdentity).where(ManifestIdentity.person_uid == uid_shared))
        assert shared_row.full_name == "Shared Renamed"
        assert uid_shared in summary_b.overlapping_uid_conflicts

        # uid_a_only: belongs ONLY to manifest A -- B's import must not touch it.
        a_only_row = db.scalar(select(ManifestIdentity).where(ManifestIdentity.person_uid == uid_a_only))
        assert a_only_row is not None and a_only_row.full_name == "A Only Person"
        a_only_elements = db.scalars(select(ManifestElement).where(ManifestElement.person_uid == uid_a_only)).all()
        assert len(a_only_elements) == 2

        # uid_b_only: newly present after B's import.
        b_only_row = db.scalar(select(ManifestIdentity).where(ManifestIdentity.person_uid == uid_b_only))
        assert b_only_row is not None and b_only_row.full_name == "B Only Person"
    finally:
        _cleanup(db, [uid_shared, uid_a_only, uid_b_only])
