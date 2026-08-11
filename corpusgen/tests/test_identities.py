"""Identity-pool invariants: variant kinds, scripted-only name collisions,
cross-identity element uniqueness, and seed determinism.
"""

import random

from faker import Faker

from corpusgen.config import MINI
from corpusgen.identities import (
    NICKNAMES,
    UNIQUE_FAMILIES,
    build_variants,
    generate_identities,
    misspell,
)


def _pool(seed: int = 42):
    rng = random.Random(seed)
    faker = Faker("en_US")
    faker.seed_instance(rng.getrandbits(64))
    return generate_identities(rng, faker, MINI)


class TestVariants:
    def test_every_identity_has_core_variant_kinds(self):
        pool = _pool()
        for identity in pool.identities:
            kinds = {v.kind for v in identity.variants}
            assert {"initials", "misspelling", "order_variant"} <= kinds

    def test_nickname_block_has_nickname_and_maiden(self):
        pool = _pool()
        for uid in pool.nickname_person_uids:
            identity = pool.by_uid[uid]
            kinds = {v.kind for v in identity.variants}
            assert identity.first_name in NICKNAMES
            assert "nickname" in kinds and "maiden" in kinds

    def test_variant_shapes(self):
        rng = random.Random(1)
        variants = {v.kind: v.value for v in build_variants(rng, "Robert", "Smith", "Jones")}
        assert variants["order_variant"] == "Smith, Robert"
        assert variants["initials"] == "R. Smith"
        assert variants["maiden"] == "Robert Jones"
        assert variants["nickname"].split()[0] in NICKNAMES["Robert"]
        assert variants["misspelling"] != "Robert Smith"

    def test_misspell_always_differs(self):
        rng = random.Random(5)
        for name in ["Smith", "Lee", "Garcia", "Oo"]:
            assert misspell(rng, name) != name


class TestPoolInvariants:
    def test_element_uniqueness_across_identities(self):
        pool = _pool()
        for family in UNIQUE_FAMILIES:
            values = [i.elements[family] for i in pool.identities]
            assert len(set(values)) == len(values), family

    def test_name_collisions_are_exactly_the_scripted_pairs(self):
        pool = _pool()
        names: dict[str, list[str]] = {}
        for i in pool.identities:
            names.setdefault(i.canonical_name, []).append(i.person_uid)
        duplicates = {frozenset(uids) for uids in names.values() if len(uids) > 1}
        assert duplicates == {frozenset(p) for p in pool.shared_name_pairs}
        assert len(pool.shared_name_pairs) == MINI.shared_name_pairs

    def test_counts(self):
        pool = _pool()
        assert len(pool.identities) == MINI.n_identities
        assert len(pool.nickname_person_uids) == MINI.nickname_cluster_persons


class TestDeterminism:
    def test_same_seed_same_pool(self):
        a, b = _pool(42), _pool(42)
        assert [i.__dict__ for i in a.identities] == [i.__dict__ for i in b.identities]

    def test_different_seed_different_values(self):
        a, b = _pool(42), _pool(43)
        assert a.identities[0].elements["ssn"] != b.identities[0].elements["ssn"]
