"""Synthetic identity pool: 160 persons, precomputed name variants, and a
pool of PII elements per person (docs/plan.md §8).

All values are seeded-Faker/RNG synthetic — never real (CLAUDE.md data-
safety rule). SSNs use valid-FORMAT ranges (area 001–899 excl. 000/666,
nonzero group/serial) so the pipeline's own validators accept them; card
numbers are Luhn-VALID; phone numbers sit in the reserved 555-01xx
fictional block; personal emails use the reserved example.com/net/org
domains. Uniqueness across identities is enforced through the shared
`used` registry, which scenarios also draw from for trap values — that is
what lets validate.py assert "no cross-identity value collisions except
where scripted".
"""

from __future__ import annotations

import datetime as dt
import random
import re
from dataclasses import dataclass, field

from faker import Faker

from corpusgen.config import CorpusConfig

# Curated nickname map — the NicknameCluster scenario draws first names
# from these keys so every cluster person has a real nickname variant.
NICKNAMES: dict[str, list[str]] = {
    "Robert": ["Bob", "Rob", "Bobby"],
    "William": ["Bill", "Will", "Billy"],
    "Elizabeth": ["Liz", "Beth", "Betsy"],
    "Margaret": ["Peggy", "Meg", "Maggie"],
    "James": ["Jim", "Jimmy"],
    "John": ["Jack", "Johnny"],
    "Michael": ["Mike", "Mikey"],
    "Katherine": ["Kate", "Katie", "Kathy"],
    "Jennifer": ["Jen", "Jenny"],
    "Richard": ["Rick", "Richie"],
    "Thomas": ["Tom", "Tommy"],
    "Christopher": ["Chris", "Kit"],
    "Patricia": ["Pat", "Patty", "Tricia"],
    "Daniel": ["Dan", "Danny"],
    "Anthony": ["Tony"],
    "Steven": ["Steve"],
    "Edward": ["Ed", "Eddie", "Ted"],
    "Susan": ["Sue", "Susie"],
    "Joseph": ["Joe", "Joey"],
    "Charles": ["Charlie", "Chuck"],
    "Andrew": ["Andy", "Drew"],
    "Nicholas": ["Nick"],
    "Samantha": ["Sam"],
    "Alexander": ["Alex", "Sasha"],
    "Benjamin": ["Ben"],
    "Rebecca": ["Becky"],
    "Deborah": ["Deb", "Debbie"],
    "Timothy": ["Tim"],
    "Matthew": ["Matt"],
    "Victoria": ["Vicky", "Tori"],
}

MEDICAL_CONDITIONS = [
    "Type 2 diabetes",
    "hypertension",
    "asthma",
    "chronic migraine",
    "hypothyroidism",
    "generalized anxiety disorder",
    "atrial fibrillation",
    "rheumatoid arthritis",
    "Crohn's disease",
    "sleep apnea",
    "chronic kidney disease stage 2",
    "major depressive disorder",
    "epilepsy",
    "psoriasis",
    "COPD",
    "osteoporosis",
    "celiac disease",
    "glaucoma",
]

_PASSWORD_WORDS = [
    "Winter", "Summer", "Harbor", "Falcon", "Maple", "Copper", "Sierra",
    "Bishop", "Juniper", "Cobalt", "Meadow", "Quartz", "Saturn", "Willow",
    "Ember", "Granite", "Lagoon", "Onyx", "Prairie", "Zephyr", "Cinder",
    "Drift", "Halcyon", "Vortex",
]

EMAIL_DOMAINS = ["example.com", "example.net", "example.org"]


@dataclass(frozen=True)
class NameVariant:
    value: str
    kind: str  # nickname | maiden | initials | misspelling | order_variant


@dataclass
class Identity:
    person_uid: str
    first_name: str
    last_name: str
    canonical_name: str
    maiden_name: str | None
    dob: str  # ISO date
    variants: list[NameVariant]
    elements: dict[str, str]


@dataclass
class IdentityPool:
    identities: list[Identity]
    by_uid: dict[str, Identity]
    shared_name_pairs: list[tuple[str, str]]  # (person_uid, person_uid)
    nickname_person_uids: list[str]
    # value-uniqueness registry, keyed by element family; scenarios draw
    # trap values through the same sets so traps can never collide with a
    # real identity's element.
    used: dict[str, set[str]] = field(default_factory=dict)


# --- element synthesis (pure, unit-tested) ---------------------------------


def make_ssn(rng: random.Random, used: set[str]) -> str:
    """Valid-format synthetic SSN: area 001–899 excluding 000/666, nonzero
    group and serial — accepted by format validators, guaranteed unique
    within `used`."""
    while True:
        area = rng.randint(1, 899)
        if area == 666:
            continue
        value = f"{area:03d}-{rng.randint(1, 99):02d}-{rng.randint(1, 9999):04d}"
        if value not in used:
            used.add(value)
            return value


def luhn_checksum(number: str) -> int:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    odd = digits[-1::-2]
    even = digits[-2::-2]
    return (sum(odd) + sum(sum(divmod(2 * d, 10)) for d in even)) % 10


def luhn_is_valid(number: str) -> bool:
    return luhn_checksum(number) == 0


def make_credit_card(rng: random.Random, used: set[str]) -> str:
    """Luhn-VALID 16-digit synthetic card, formatted 4-4-4-4 with spaces."""
    while True:
        prefix = rng.choice(["4", "51", "52", "6011"])
        body = prefix + "".join(str(rng.randint(0, 9)) for _ in range(15 - len(prefix)))
        check = (10 - luhn_checksum(body + "0")) % 10
        digits = body + str(check)
        value = " ".join(digits[i : i + 4] for i in range(0, 16, 4))
        if value not in used:
            used.add(value)
            return value


def make_luhn_invalid_card(rng: random.Random, used: set[str]) -> str:
    """Card-LIKE number that fails the Luhn check — FalsePositiveTraps
    material. Built by corrupting a valid card's check digit."""
    valid = make_credit_card(rng, used)
    bumped = valid[:-1] + str((int(valid[-1]) + rng.randint(1, 9)) % 10)
    assert not luhn_is_valid(bumped)
    used.add(bumped)
    return bumped


def _unique(rng: random.Random, used: set[str], make) -> str:
    while True:
        value = make()
        if value not in used:
            used.add(value)
            return value


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def make_phone(rng: random.Random, used: set[str]) -> str:
    # 555-01xx is the reserved fictional exchange block.
    return _unique(
        rng, used, lambda: f"({rng.randint(201, 979)}) 555-{rng.randint(100, 199):04d}"
    )


def make_dob(rng: random.Random) -> str:
    start = dt.date(1955, 1, 1)
    return (start + dt.timedelta(days=rng.randrange(0, 365 * 50))).isoformat()


# --- name variant synthesis (pure, unit-tested) ----------------------------


def misspell(rng: random.Random, word: str) -> str:
    """One realistic typo on an interior letter: adjacent swap, drop, or
    doubling. Always returns something different from `word`."""
    if len(word) < 3:  # too short for interior ops (e.g. "Ng", "Oo")
        return word + word[-1]
    for _ in range(20):
        op = rng.choice(["swap", "drop", "double"])
        i = rng.randrange(1, len(word) - 1)
        if op == "swap":
            cand = word[:i] + word[i + 1] + word[i] + word[i + 2 :]
        elif op == "drop" and len(word) > 4:
            cand = word[:i] + word[i + 1 :]
        else:
            cand = word[:i] + word[i] + word[i:]
        if cand != word:
            return cand
    return word + word[-1]  # degenerate fallback (e.g. all-same-letter word)


def build_variants(
    rng: random.Random,
    first: str,
    last: str,
    maiden: str | None,
) -> list[NameVariant]:
    variants: list[NameVariant] = []
    if first in NICKNAMES:
        variants.append(NameVariant(f"{rng.choice(NICKNAMES[first])} {last}", "nickname"))
    if maiden:
        variants.append(NameVariant(f"{first} {maiden}", "maiden"))
    variants.append(NameVariant(f"{first[0]}. {last}", "initials"))
    variants.append(NameVariant(f"{first} {misspell(rng, last)}", "misspelling"))
    variants.append(NameVariant(f"{last}, {first}", "order_variant"))
    return variants


# --- pool generation -------------------------------------------------------


def _make_identity(
    rng: random.Random,
    faker: Faker,
    uid: str,
    first: str,
    last: str,
    used: dict[str, set[str]],
    force_maiden: bool,
    maiden_fraction: float,
) -> Identity:
    maiden: str | None = None
    if force_maiden or rng.random() < maiden_fraction:
        while True:
            maiden = faker.last_name()
            if maiden != last:
                break

    email = _unique(
        rng,
        used["email"],
        lambda: f"{_slug(first)}.{_slug(last)}{rng.randint(1, 999)}@{rng.choice(EMAIL_DOMAINS)}",
    )
    address = _unique(
        rng,
        used["address"],
        lambda: f"{faker.street_address()}, {faker.city()}, {faker.state_abbr()} {faker.zipcode()}",
    )
    elements: dict[str, str] = {
        "ssn": make_ssn(rng, used["ssn"]),
        "credit_card": make_credit_card(rng, used["credit_card"]),
        "phone": make_phone(rng, used["phone"]),
        "email": email,
        "address": address,
        "drivers_license": _unique(
            rng,
            used["drivers_license"],
            lambda: f"{rng.choice('ABCDEFGHJKLMNPRSTUVWXYZ')}{rng.randint(1000000, 9999999)}",
        ),
        "passport": _unique(
            rng, used["passport"], lambda: str(rng.randint(100000000, 999999999))
        ),
        "financial_account": _unique(
            rng, used["financial_account"], lambda: str(rng.randint(10**9, 10**11 - 1))
        ),
        # Medical conditions legitimately repeat across people — excluded
        # from the cross-identity collision check (validate.py).
        "medical": rng.choice(MEDICAL_CONDITIONS),
        "username": _unique(
            rng,
            used["username"],
            lambda: f"{_slug(first)[0]}{_slug(last)}{rng.randint(10, 99)}",
        ),
        "password": _unique(
            rng,
            used["password"],
            lambda: f"{rng.choice(_PASSWORD_WORDS)}{rng.randint(10, 99)}{rng.choice('!@#$%')}",
        ),
        "employee_id": _unique(
            rng, used["employee_id"], lambda: f"E{rng.randint(10000, 99999)}"
        ),
    }
    return Identity(
        person_uid=uid,
        first_name=first,
        last_name=last,
        canonical_name=f"{first} {last}",
        maiden_name=maiden,
        dob=make_dob(rng),
        variants=build_variants(rng, first, last, maiden),
        elements=elements,
    )


UNIQUE_FAMILIES = [
    "ssn", "credit_card", "phone", "email", "address", "drivers_license",
    "passport", "financial_account", "username", "password", "employee_id",
]


def generate_identities(rng: random.Random, faker: Faker, cfg: CorpusConfig) -> IdentityPool:
    """Deterministic pool: shared-name pairs first, then the nickname-capable
    block, then Faker-named remainder. Canonical names are unique EXCEPT the
    scripted shared-name pairs."""
    used: dict[str, set[str]] = {family: set() for family in UNIQUE_FAMILIES}
    taken_names: set[str] = set()
    identities: list[Identity] = []
    shared_pairs: list[tuple[str, str]] = []
    nickname_uids: list[str] = []

    def next_uid() -> str:
        return f"P{len(identities) + 1:04d}"

    # 1) SharedName pairs: two different people, identical canonical name.
    for _ in range(cfg.shared_name_pairs):
        while True:
            first, last = faker.first_name(), faker.last_name()
            if f"{first} {last}" not in taken_names:
                break
        taken_names.add(f"{first} {last}")
        pair = []
        for _ in range(2):
            ident = _make_identity(
                rng, faker, next_uid(), first, last, used, False, cfg.maiden_fraction
            )
            identities.append(ident)
            pair.append(ident.person_uid)
        shared_pairs.append((pair[0], pair[1]))

    # 2) Nickname-cluster block: first names guaranteed to be in NICKNAMES;
    #    maiden forced so the cluster can exercise every variant kind.
    nickname_firsts = sorted(NICKNAMES)
    for _ in range(cfg.nickname_cluster_persons):
        while True:
            first = rng.choice(nickname_firsts)
            last = faker.last_name()
            if f"{first} {last}" not in taken_names:
                break
        taken_names.add(f"{first} {last}")
        ident = _make_identity(rng, faker, next_uid(), first, last, used, True, cfg.maiden_fraction)
        identities.append(ident)
        nickname_uids.append(ident.person_uid)

    # 3) Remainder from Faker.
    while len(identities) < cfg.n_identities:
        first, last = faker.first_name(), faker.last_name()
        if f"{first} {last}" in taken_names:
            continue
        taken_names.add(f"{first} {last}")
        identities.append(
            _make_identity(rng, faker, next_uid(), first, last, used, False, cfg.maiden_fraction)
        )

    return IdentityPool(
        identities=identities,
        by_uid={i.person_uid: i for i in identities},
        shared_name_pairs=shared_pairs,
        nickname_person_uids=nickname_uids,
        used=used,
    )
