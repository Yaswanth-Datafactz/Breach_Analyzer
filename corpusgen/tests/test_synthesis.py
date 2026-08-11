"""Element-synthesis validity: SSNs must satisfy the pipeline's own format
validators (area 001-899 excl. 000/666, nonzero group/serial) and cards
must be genuinely Luhn-valid — otherwise the corpus would train the
detectors on values they are built to reject.
"""

import random
import re

from corpusgen.identities import (
    luhn_is_valid,
    make_credit_card,
    make_luhn_invalid_card,
    make_phone,
    make_ssn,
)

_SSN_RE = re.compile(r"^(\d{3})-(\d{2})-(\d{4})$")


class TestSsnSynthesis:
    def test_format_and_ranges_over_many_draws(self):
        rng = random.Random(7)
        used: set[str] = set()
        for _ in range(500):
            ssn = make_ssn(rng, used)
            m = _SSN_RE.match(ssn)
            assert m, ssn
            area, group, serial = int(m.group(1)), int(m.group(2)), int(m.group(3))
            assert 1 <= area <= 899 and area != 666
            assert group != 0
            assert serial != 0

    def test_uniqueness_within_registry(self):
        rng = random.Random(7)
        used: set[str] = set()
        values = [make_ssn(rng, used) for _ in range(500)]
        assert len(set(values)) == 500


class TestLuhnSynthesis:
    def test_generated_cards_are_luhn_valid(self):
        rng = random.Random(11)
        used: set[str] = set()
        for _ in range(200):
            card = make_credit_card(rng, used)
            digits = card.replace(" ", "")
            assert len(digits) == 16 and digits.isdigit()
            assert luhn_is_valid(card)

    def test_invalid_maker_fails_luhn(self):
        rng = random.Random(11)
        used: set[str] = set()
        for _ in range(100):
            assert not luhn_is_valid(make_luhn_invalid_card(rng, used))

    def test_known_luhn_vectors(self):
        assert luhn_is_valid("4539 1488 0343 6467")
        assert not luhn_is_valid("4539 1488 0343 6468")


class TestPhoneSynthesis:
    def test_reserved_fictional_block(self):
        rng = random.Random(3)
        used: set[str] = set()
        for _ in range(100):
            assert re.match(r"^\(\d{3}\) 555-01\d{2}$", make_phone(rng, used))
