"""Candidate-pair generation (docs/plan.md D5: block before you score).
Only mentions sharing at least one block key are ever scored, which is
what keeps ER subquadratic -- test_er_blocking.py proves the pair count
on 1000 synthetic mentions is a small fraction of n^2/2, and §12 names
embedding blocking as the 1M-doc successor to these exact keys.

Block families:
1. strong:{type}:{value} -- exact normalized strong identifier, the
   in-DB (element_type, value_normalized) index made in-memory.
2. phonetic:{mp(surname)}:{mp(canonical_given)} -- jellyfish metaphone
   over the nickname-canonicalized given, so "Bob Smith" and "Robert
   Smith" share a key. NOTE jellyfish is SINGLE metaphone. Measured
   against corpusgen's actual misspelling generator (seed 42, n=300):
   single metaphone misses 30.7% of misspelled-surname keys and the
   'metaphone' double-metaphone lib still misses 29.0% -- the generator's
   swap/drop/double typos change pronunciation, so a second phonetic
   algorithm is not the fix and the dependency was measured OUT.
3. typo:{surname-deletion-key}:{given-initial} -- symmetric-delete
   (SymSpell-style) keys: a surname emits itself plus every single-char-
   deletion string. An adjacent swap, char drop, or char doubling ALWAYS
   leaves a shared deletion key, so this covers corpusgen's misspelling
   generator at 100% on the same measurement (3/300 near-misses were
   short-name edge cases fixed by the len>=3 rule below). Qualified by
   canonical-given initials to keep blocks small.
4. initial:{mp(surname)}:{initial} -- initials variants ("J. Smith" ↔
   "James Smith") meet full-given mentions here.
5. dob:{iso} -- same-DOB block; also the maiden-name safety net (a
   surname change breaks every name key, but DOB or a shared strong
   identifier still pairs the variants -- treated as candidates, never
   auto-matched, per D5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

import jellyfish

from app.services.er.normalize import ErMention

# Strong identifier element types (plan §10's match ladder). ssn_last4 is
# deliberately absent: four digits collide across persons, so it is a
# scoring feature, never a block.
STRONG_IDENTIFIER_TYPES: tuple[str, ...] = (
    "ssn",
    "email",
    "phone",
    "financial_account",
    "credit_card",
    "employee_id",
    "drivers_license",
    "passport",
)

# Name/dob blocks larger than this are skipped (counted in stats): a
# 500-strong "same surname key" block would reintroduce the quadratic
# blowup blocking exists to prevent. Strong-identifier blocks are NEVER
# capped -- an identical normalized SSN is decisive however many mentions
# carry it (the 80-row bulk sheet is the design case).
DEFAULT_MAX_NAME_BLOCK = 200


def surname_deletion_keys(surname: str) -> frozenset[str]:
    """The surname plus every single-character deletion of it (len>=3
    only -- deleting from 2-char names degenerates to single letters).
    Two strings within one swap/drop/double always share a key."""
    if len(surname) < 3:
        return frozenset({surname})
    return frozenset({surname}) | {
        surname[:i] + surname[i + 1 :] for i in range(len(surname))
    }


@dataclass
class BlockingStats:
    n_mentions: int = 0
    n_pairs: int = 0
    n_blocks: int = 0
    oversized_blocks: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class BlockingResult:
    """pairs maps an ordered (left_id, right_id) tuple -- lexicographic,
    left < right -- to the set of block keys that produced it (persisted
    into er_decisions.features for explainability)."""

    pairs: dict[tuple[str, str], set[str]]
    stats: BlockingStats


def _mention_keys(mention: ErMention) -> Iterable[tuple[str, bool]]:
    """Yield (block_key, is_strong) for one mention."""
    for element_type in STRONG_IDENTIFIER_TYPES:
        for value in mention.elements.get(element_type, ()):
            yield f"strong:{element_type}:{value}", True

    name = mention.name
    if name is not None and name.surname and name.given:
        surname_mp = jellyfish.metaphone(name.surname)
        initials = {c[0] for c in name.given_canonicals if c}
        if name.given_is_initial:
            # An initial-only given joins the initial block; it has no
            # phonetic or typo key of its own (nothing to sound out).
            if surname_mp:
                yield f"initial:{surname_mp}:{name.given}", False
        else:
            for canonical in name.given_canonicals:
                given_mp = jellyfish.metaphone(canonical)
                if surname_mp and given_mp:
                    yield f"phonetic:{surname_mp}:{given_mp}", False
            if surname_mp:
                for initial in initials:
                    yield f"initial:{surname_mp}:{initial}", False
            for key in surname_deletion_keys(name.surname):
                for initial in initials:
                    yield f"typo:{key}:{initial}", False

    if mention.dob:
        yield f"dob:{mention.dob}", False


def generate_candidate_pairs(
    mentions: Sequence[ErMention],
    max_name_block_size: int = DEFAULT_MAX_NAME_BLOCK,
) -> BlockingResult:
    """Union of within-block pairs across all block families. Pure and
    deterministic: same mentions in, same pairs out, regardless of input
    order (pair keys are sorted mention-id tuples)."""
    blocks: dict[str, list[str]] = {}
    strong_keys: set[str] = set()
    for mention in mentions:
        for key, is_strong in _mention_keys(mention):
            blocks.setdefault(key, []).append(mention.mention_id)
            if is_strong:
                strong_keys.add(key)

    stats = BlockingStats(n_mentions=len(mentions), n_blocks=len(blocks))
    pairs: dict[tuple[str, str], set[str]] = {}
    for key, member_ids in blocks.items():
        # Same mention can emit one key twice (two equal canonicals cannot
        # happen, but two elements of one type sharing a normalized value
        # can) -- dedupe before pairing.
        unique_ids = sorted(set(member_ids))
        if len(unique_ids) < 2:
            continue
        if key not in strong_keys and len(unique_ids) > max_name_block_size:
            stats.oversized_blocks.append((key, len(unique_ids)))
            continue
        for i, left in enumerate(unique_ids):
            for right in unique_ids[i + 1 :]:
                pairs.setdefault((left, right), set()).add(key)

    stats.n_pairs = len(pairs)
    return BlockingResult(pairs=pairs, stats=stats)
