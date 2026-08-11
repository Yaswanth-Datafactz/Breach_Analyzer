"""Synthetic breach-corpus generator (docs/plan.md §8 — scored deliverable).

Every document AND its manifest entry are emitted together by scenario
objects (docs/plan.md Decision D6), so the accuracy answer key can never
drift from the corpus. One seeded `random.Random` (threaded explicitly —
no module-level randomness anywhere in this package) plus a Faker seeded
from it drives identities, content, and dates: the same seed reproduces
the manifest byte-for-byte.

Run: python -m corpusgen --seed 42 --out data/corpus --manifest data/manifest.json
"""

__version__ = "0.1.0"
