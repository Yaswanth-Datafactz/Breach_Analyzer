"""Per-pair feature vector (docs/plan.md D5): everything scoring weighs,
computed once, serialized whole into er_decisions.features so the
adjudicator's citations and the review UI's side-by-side evidence render
from the exact numbers the decision used.

Design notes that earn their keep:
- Name similarity is COMPONENT-wise (surname, given, token-sort), not one
  fuzzy score over the full string: measured on this corpus,
  JaroWinkler("elizabeth tran", "elizabeth nguyen") = 0.89 -- a single
  full-string score would call two different surnames a near-match purely
  on the shared given's prefix boost. Splitting components lets scoring
  weigh the surname on its own.
- surname_change_candidate is the maiden-name signal: given names agree
  (directly or via the nickname map) while surnames clearly differ.
  It is evidence FOR adjudication, never for auto-match (D5), and
  scoring gives it no positive weight -- a maiden variant merges only
  when a strong identifier or DOB corroborates it.
- ssn_last4 compatibility only fires when at least one side LACKS the
  full SSN -- when both have full SSNs the shared/conflicting-strong
  features already carry the verdict and last4 would double-count it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from app.services.er.blocking import STRONG_IDENTIFIER_TYPES
from app.services.er.normalize import ErMention

# Surname JaroWinkler at or above this counts as "same surname (allowing
# typos)"; below it, a given-name match flips surname_change_candidate.
# Measured against corpusgen's misspelling generator (seed 42, n=2000,
# faker surnames): min JW by surname length is 0.880 (len 4), 0.830
# (len 6), so 0.80 clears every generated typo on surnames of length
# >= 4; a 3-letter adjacent swap ("Lam" -> "Lma", JW 0.556) falls below
# ANY usable threshold -- those pairs are still surfaced by blocking's
# deletion keys and merge via shared identifiers, they just carry the
# surname_change_candidate flag instead of typo-level surname agreement.
# Genuinely different surnames sit far below ("tran" vs "nguyen": 0.49).
SURNAME_TYPO_THRESHOLD = 0.80


@dataclass(frozen=True)
class PairFeatures:
    shared_strong_types: tuple[str, ...]
    shared_strong_count: int
    conflicting_strong_types: tuple[str, ...]
    # name components (0.0 when either side has no usable name)
    name_token_sort: float
    name_jaro_winkler: float
    surname_similarity: float
    given_similarity: float
    name_similarity: float
    surname_exact: bool
    given_exact: bool
    nickname_hit: bool
    initials_compatible: bool
    surname_change_candidate: bool
    # DOB
    dob_match: bool
    dob_conflict: bool
    # partial-identifier bridges
    ssn_last4_compatible: bool
    ssn_last4_conflict: bool
    # context
    address_overlap: float
    same_doc: bool

    @property
    def has_corroboration(self) -> bool:
        """Any evidence beyond the name string itself -- the gate the
        name-only cap (scoring.py) keys on."""
        return (
            self.shared_strong_count > 0
            or self.dob_match
            or self.ssn_last4_compatible
            or self.address_overlap >= 0.5
            or self.same_doc
        )

    def to_dict(self) -> dict:
        """JSONB-ready (er_decisions.features)."""
        data = asdict(self)
        data["shared_strong_types"] = list(self.shared_strong_types)
        data["conflicting_strong_types"] = list(self.conflicting_strong_types)
        data["has_corroboration"] = self.has_corroboration
        return data


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _address_overlap(a: ErMention, b: ErMention) -> float:
    """Max token-jaccard across the two mentions' address values --
    tolerant of unit numbers and city/state formatting drift."""
    best = 0.0
    for addr_a in a.elements.get("address", ()):
        tokens_a = set(addr_a.split())
        for addr_b in b.elements.get("address", ()):
            best = max(best, _jaccard(tokens_a, set(addr_b.split())))
    return best


def _last4_values(mention: ErMention) -> frozenset[str]:
    values = set(mention.elements.get("ssn_last4", ()))
    values |= {ssn[-4:] for ssn in mention.elements.get("ssn", ()) if len(ssn) >= 4}
    return frozenset(values)


def compute_features(a: ErMention, b: ErMention) -> PairFeatures:
    # --- strong identifiers ---
    shared: list[str] = []
    conflicting: list[str] = []
    for element_type in STRONG_IDENTIFIER_TYPES:
        values_a = a.elements.get(element_type)
        values_b = b.elements.get(element_type)
        if not values_a or not values_b:
            continue
        if values_a & values_b:
            shared.append(element_type)
        else:
            # Both sides carry this identifier type and NO value overlaps:
            # e.g. two different SSNs. The hard-conflict input (D5).
            conflicting.append(element_type)

    # --- ssn_last4 bridge (partial-identifier scenario) ---
    both_have_full_ssn = bool(a.elements.get("ssn")) and bool(b.elements.get("ssn"))
    last4_a, last4_b = _last4_values(a), _last4_values(b)
    last4_compatible = last4_conflict = False
    if last4_a and last4_b and not both_have_full_ssn:
        if last4_a & last4_b:
            last4_compatible = True
        else:
            last4_conflict = True

    # --- names ---
    token_sort = jaro = surname_sim = given_sim = name_sim = 0.0
    surname_exact = given_exact = nickname_hit = initials_compat = False
    surname_change = False
    name_a, name_b = a.name, b.name
    if name_a is not None and name_b is not None and name_a.full and name_b.full:
        token_sort = fuzz.token_sort_ratio(name_a.full, name_b.full) / 100.0
        jaro = JaroWinkler.normalized_similarity(name_a.full, name_b.full)
        if name_a.surname and name_b.surname:
            surname_sim = JaroWinkler.normalized_similarity(
                name_a.surname, name_b.surname
            )
            surname_exact = name_a.surname == name_b.surname
        if name_a.given and name_b.given:
            given_exact = name_a.given == name_b.given
            nickname_hit = (
                not given_exact
                and not name_a.given_is_initial
                and not name_b.given_is_initial
                and bool(name_a.given_canonicals & name_b.given_canonicals)
            )
            initials_compat = (
                name_a.given_is_initial != name_b.given_is_initial
                and (
                    (name_a.given_is_initial and name_a.given == name_b.given[0])
                    or (name_b.given_is_initial and name_b.given == name_a.given[0])
                )
            )
            if given_exact or nickname_hit or initials_compat:
                given_sim = 1.0
            else:
                given_sim = JaroWinkler.normalized_similarity(
                    name_a.given, name_b.given
                )
        # Component-weighted composite: surname carries the most identity
        # signal, token_sort keeps middle names/reorders honest.
        name_sim = 0.45 * surname_sim + 0.35 * given_sim + 0.20 * token_sort
        surname_change = (
            (given_exact or nickname_hit)
            and bool(name_a.surname)
            and bool(name_b.surname)
            and surname_sim < SURNAME_TYPO_THRESHOLD
        )

    # --- DOB ---
    dob_match = dob_conflict = False
    if a.dob and b.dob:
        dob_match = a.dob == b.dob
        dob_conflict = not dob_match

    return PairFeatures(
        shared_strong_types=tuple(shared),
        shared_strong_count=len(shared),
        conflicting_strong_types=tuple(conflicting),
        name_token_sort=round(token_sort, 4),
        name_jaro_winkler=round(jaro, 4),
        surname_similarity=round(surname_sim, 4),
        given_similarity=round(given_sim, 4),
        name_similarity=round(name_sim, 4),
        surname_exact=surname_exact,
        given_exact=given_exact,
        nickname_hit=nickname_hit,
        initials_compatible=initials_compat,
        surname_change_candidate=surname_change,
        dob_match=dob_match,
        dob_conflict=dob_conflict,
        ssn_last4_compatible=last4_compatible,
        ssn_last4_conflict=last4_conflict,
        address_overlap=round(_address_overlap(a, b), 4),
        same_doc=a.doc_id == b.doc_id,
    )
