"""Name and value normalization -- the single shared path everything
downstream (blocking, scoring, exposure, the accuracy scorer) calls
(docs/plan.md D5; extraction/schemas.py's contract that values stay
verbatim at the model boundary and are parsed exactly once, here).

Name handling covers the corpusgen variant kinds the manifest scores us
against (identities.build_variants): nickname (curated map below),
"Last, First" reorder, single-initial givens, misspelled surnames
(similarity/blocking's job -- normalization just cleans), and maiden-name
surname changes. Maiden handling is deliberately NOT a normalization:
a surname change is surfaced as a candidate-variant *feature*
(features.surname_change_candidate) and never collapses two names into
one normalized form -- collapsing here would make "Elizabeth Tran" and
"Elizabeth Nguyen" indistinguishable before scoring ever saw them, which
is exactly the auto-match D5 forbids.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

# --- nickname canonicalization ---------------------------------------------

# Curated canonical-given -> nicknames map. Superset of corpusgen/
# identities.py's NICKNAMES (the manifest's nickname variants are drawn
# from that map, so covering it is the floor -- test_er_normalize.py
# asserts the coverage against the real corpusgen module). Kept as its own
# curated copy on purpose: production ER must not import the test-corpus
# generator, and this map legitimately knows more than the generator does.
_CANONICAL_NICKNAMES: dict[str, list[str]] = {
    "robert": ["bob", "rob", "bobby", "robbie"],
    "william": ["bill", "will", "billy", "willie"],
    "elizabeth": ["liz", "beth", "betsy", "eliza", "lizzie"],
    "margaret": ["peggy", "meg", "maggie", "marge"],
    "james": ["jim", "jimmy", "jamie"],
    "john": ["jack", "johnny", "jon"],
    "michael": ["mike", "mikey"],
    "katherine": ["kate", "katie", "kathy", "kit"],
    "jennifer": ["jen", "jenny"],
    "richard": ["rick", "richie", "dick", "ricky"],
    "thomas": ["tom", "tommy"],
    "christopher": ["chris", "kit", "topher"],
    "patricia": ["pat", "patty", "tricia", "trish"],
    "daniel": ["dan", "danny"],
    "anthony": ["tony"],
    "steven": ["steve"],
    "stephen": ["steve"],
    "edward": ["ed", "eddie", "ted", "ned"],
    "theodore": ["ted", "teddy", "theo"],
    "susan": ["sue", "susie"],
    "joseph": ["joe", "joey"],
    "charles": ["charlie", "chuck"],
    "andrew": ["andy", "drew"],
    "nicholas": ["nick"],
    "samantha": ["sam"],
    "samuel": ["sam", "sammy"],
    "alexander": ["alex", "sasha"],
    "alexandra": ["alex", "sasha", "lexi"],
    "benjamin": ["ben", "benny"],
    "rebecca": ["becky"],
    "deborah": ["deb", "debbie"],
    "timothy": ["tim"],
    "matthew": ["matt"],
    "victoria": ["vicky", "tori"],
    "patrick": ["pat", "paddy"],
    "christine": ["chris", "chrissy"],
    "christina": ["chris", "tina"],
}

# Inverted: nickname -> every canonical it may stand for ("sam" is
# ambiguous between samantha/samuel -- compatibility is set intersection,
# never a forced single expansion).
NICKNAME_TO_CANONICALS: dict[str, frozenset[str]] = {}
for _canonical, _nicks in _CANONICAL_NICKNAMES.items():
    for _nick in _nicks:
        NICKNAME_TO_CANONICALS[_nick] = NICKNAME_TO_CANONICALS.get(
            _nick, frozenset()
        ) | {_canonical}


def given_name_canonicals(given: str) -> frozenset[str]:
    """Every canonical form a given name may stand for, itself included.
    "bob" -> {bob, robert}; "robert" -> {robert}. Two givens are nickname-
    compatible iff their canonical sets intersect ("bob" vs "rob" meet at
    "robert")."""
    g = given.strip().casefold()
    if not g:
        return frozenset()
    return NICKNAME_TO_CANONICALS.get(g, frozenset()) | {g}


# --- name normalization ----------------------------------------------------

_SUFFIX_TOKENS = {"jr", "sr", "ii", "iii", "iv", "v", "md", "phd", "esq", "dds"}
_APOSTROPHES = re.compile(r"[''`’]")
_NON_WORD = re.compile(r"[^\w\s]|_")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class NormalizedName:
    """The normalized decomposition blocking and features consume. `full`
    is the canonical "given [middle...] surname" string; `given_canonicals`
    already folds the nickname map in, so callers never re-derive it."""

    raw: str
    full: str
    tokens: tuple[str, ...]
    given: str | None
    surname: str | None
    given_is_initial: bool
    given_canonicals: frozenset[str]
    was_reordered: bool


def normalize_name(raw: str) -> NormalizedName:
    """casefold -> "Last, First" reorder -> punctuation strip (apostrophes
    removed, other punctuation becomes a token break) -> suffix drop ->
    whitespace collapse. Initial *detection* happens here (a one-letter
    given); initial *expansion* is deliberately left to features/blocking
    ("J." could be James or John -- an attempt, never an assertion)."""
    text = raw.strip()
    was_reordered = False
    if "," in text:
        left, _, right = text.partition(",")
        if left.strip() and right.strip():
            # "Lam, Benjamin" -> "Benjamin Lam" (order_variant kind).
            text = f"{right.strip()} {left.strip()}"
            was_reordered = True
    text = _APOSTROPHES.sub("", text.casefold())
    text = _NON_WORD.sub(" ", text)
    tokens = tuple(
        t for t in _WHITESPACE.split(text) if t and t not in _SUFFIX_TOKENS
    )
    given = tokens[0] if tokens else None
    surname = tokens[-1] if len(tokens) >= 2 else None
    given_is_initial = given is not None and len(given) == 1
    if given is None:
        canonicals: frozenset[str] = frozenset()
    elif given_is_initial:
        # A bare initial has no nickname semantics -- keep only itself.
        canonicals = frozenset({given})
    else:
        canonicals = given_name_canonicals(given)
    return NormalizedName(
        raw=raw,
        full=" ".join(tokens),
        tokens=tokens,
        given=given,
        surname=surname,
        given_is_initial=given_is_initial,
        given_canonicals=canonicals,
        was_reordered=was_reordered,
    )


# --- per-element-type value normalization ----------------------------------

_DOB_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%Y/%m/%d",
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B %Y",
    "%m/%d/%y",
)


def normalize_dob(raw: str) -> str | None:
    """Parse to ISO or admit failure with None -- a half-parsed date that
    compares unequal to its ISO twin would silently break the DOB block
    and the dob_match/conflict features."""
    text = raw.strip()
    for fmt in _DOB_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _digits(raw: str) -> str:
    return re.sub(r"\D", "", raw)


def _normalize_phone(raw: str) -> str:
    digits = _digits(raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]  # US country code prefix
    return digits


def _normalize_email(raw: str) -> str:
    text = raw.strip().strip(".,;:<>()[]")
    if text.casefold().startswith("mailto:"):
        text = text[7:]
    return text.casefold()


def _normalize_alnum_upper(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", raw.upper())


def _normalize_text(raw: str) -> str:
    text = _NON_WORD.sub(" ", raw.casefold())
    return _WHITESPACE.sub(" ", text).strip()


def normalize_value(element_type: str, raw: str) -> str:
    """value_raw -> value_normalized, per element type (the §4
    pii_elements.value_normalized column and the (element_type,
    value_normalized) blocking index both hold exactly this)."""
    if element_type in ("ssn", "ssn_last4", "credit_card", "financial_account"):
        return _digits(raw)
    if element_type == "phone":
        return _normalize_phone(raw)
    if element_type == "email":
        return _normalize_email(raw)
    if element_type == "dob":
        return normalize_dob(raw) or _normalize_text(raw)
    if element_type in ("employee_id", "drivers_license", "passport"):
        return _normalize_alnum_upper(raw)
    if element_type == "name":
        return normalize_name(raw).full
    if element_type == "credential":
        # Passwords are case-sensitive; usernames nearly always compare
        # case-insensitively but a lost casefold only weakens one weak
        # feature -- strip() is the safe common denominator.
        return raw.strip()
    return _normalize_text(raw)  # address, medical, anything future


# --- the in-memory mention ER operates on ----------------------------------


@dataclass
class ErMention:
    """One person-reference with its co-occurring elements, fully
    normalized -- the pure-function layer's unit (mirrors the `mentions`
    row + its attached `pii_elements`; Thursday's DB wiring constructs
    these from rows via build_mention and nothing else changes)."""

    mention_id: str
    doc_id: str
    name: NormalizedName | None
    dob: str | None  # ISO, already normalized
    # element_type -> set of value_normalized for that type.
    elements: dict[str, frozenset[str]] = field(default_factory=dict)


def build_mention(
    mention_id: str,
    doc_id: str,
    name_raw: str | None = None,
    dob_raw: str | None = None,
    elements: list[tuple[str, str]] | None = None,
) -> ErMention:
    """Normalize raw inputs into an ErMention. `elements` is (element_type,
    value_raw) pairs; dob-typed elements fold into the `dob` field (first
    parseable wins) AND stay listed as elements, matching how the pipeline
    stores dob both as a mention attribute and a pii_element."""
    by_type: dict[str, set[str]] = {}
    dob_iso = normalize_dob(dob_raw) if dob_raw else None
    for element_type, value_raw in elements or []:
        normalized = normalize_value(element_type, value_raw)
        if not normalized:
            continue
        by_type.setdefault(element_type, set()).add(normalized)
        if element_type == "dob" and dob_iso is None:
            dob_iso = normalize_dob(value_raw)
    return ErMention(
        mention_id=mention_id,
        doc_id=doc_id,
        name=normalize_name(name_raw) if name_raw and name_raw.strip() else None,
        dob=dob_iso,
        elements={t: frozenset(v) for t, v in by_type.items()},
    )
