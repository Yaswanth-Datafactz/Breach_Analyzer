"""Manifest round-trip and schema shape: what corpusgen writes is what the
accuracy harness will read back.
"""

import random

from faker import Faker

from corpusgen import manifest as manifest_mod
from corpusgen.config import MINI
from corpusgen.identities import generate_identities
from corpusgen.manifest import ManifestDocument


def _build():
    rng = random.Random(42)
    faker = Faker("en_US")
    faker.seed_instance(rng.getrandbits(64))
    pool = generate_identities(rng, faker, MINI)
    person = pool.identities[0]
    doc = ManifestDocument(
        doc_uid="D0001",
        filename="D0001_hr_memo.pdf",
        file_class="pdf_digital",
        renderer="digital_pdf",
        scenario_tags=["background"],
        problem=None,
        plantings=[
            {
                "person_uid": person.person_uid,
                "element_type": "ssn",
                "value": person.elements["ssn"],
                "location": {"page": 1},
                "presentation": "prose",
            }
        ],
    )
    return manifest_mod.build(42, MINI, pool, [doc])


class TestManifestShape:
    def test_top_level_schema(self):
        data = _build()
        assert set(data) == {
            "seed", "profile", "generated_counts", "scenario_script",
            "identities", "documents",
        }
        assert data["seed"] == 42 and data["profile"] == "mini"
        counts = data["generated_counts"]
        assert counts["documents"] == 1 and counts["plantings"] == 1
        assert counts["by_file_class"] == {"pdf_digital": 1}

    def test_identity_entry_shape(self):
        entry = _build()["identities"][0]
        assert set(entry) == {
            "person_uid", "canonical_name", "name_variants", "dob", "elements",
        }
        assert all(set(v) == {"value", "kind"} for v in entry["name_variants"])

    def test_document_entry_shape(self):
        entry = _build()["documents"][0]
        assert set(entry) == {
            "doc_uid", "filename", "file_class", "renderer",
            "scenario_tags", "problem", "attachments", "plantings",
        }
        assert entry["problem"] is None
        assert entry["attachments"] is None


class TestRoundTrip:
    def test_write_then_load_is_identical(self, tmp_path):
        data = _build()
        path = tmp_path / "manifest.json"
        manifest_mod.write(path, data)
        assert manifest_mod.load(path) == data

    def test_write_is_byte_stable(self, tmp_path):
        data = _build()
        p1, p2 = tmp_path / "m1.json", tmp_path / "m2.json"
        manifest_mod.write(p1, data)
        manifest_mod.write(p2, _build())
        assert p1.read_bytes() == p2.read_bytes()
