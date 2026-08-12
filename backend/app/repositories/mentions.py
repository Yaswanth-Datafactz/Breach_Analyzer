"""Data access for `mentions` (UC2's repositories layer) -- the unit ER
clusters (docs/plan.md §4). Written by B2's extraction service (LLM tiers
+ the deterministic sheet extractor); read by B3's ER wiring, which builds
ErMention objects from these rows."""

from __future__ import annotations

import uuid
from datetime import date

import jellyfish
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Mention
from app.services.er.normalize import normalize_name


def phonetic_key(name_raw: str) -> str | None:
    """The stored blocking key: metaphone of the normalized SURNAME (the
    key er/blocking.py's phonetic family is built on). Falls back to the
    full normalized name for single-token names. §14b measured metaphone
    as insufficient ALONE for misspellings -- blocking adds
    symmetric-delete keys at query time; this column serves the phonetic
    family and the §4 index."""
    normalized = normalize_name(name_raw)
    basis = normalized.surname or normalized.full
    if not basis:
        return None
    key = jellyfish.metaphone(basis)
    return (key or basis)[:50]


class MentionRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, mention_id: uuid.UUID) -> Mention | None:
        return self.db.get(Mention, mention_id)

    def create(
        self,
        *,
        document_id: uuid.UUID,
        passage_id: uuid.UUID,
        name_raw: str,
        detector: str,
        dob: date | None = None,
        features: dict | None = None,
        confidence: float | None = None,
    ) -> Mention:
        normalized = normalize_name(name_raw)
        mention = Mention(
            document_id=document_id,
            passage_id=passage_id,
            name_raw=name_raw[:300],
            name_normalized=normalized.full[:300],
            name_phonetic=phonetic_key(name_raw),
            dob=dob,
            features=features or {},
            detector=detector,
            confidence=confidence,
        )
        self.db.add(mention)
        self.db.flush()  # populate mention.id without committing yet
        return mention

    def set_dob(self, mention: Mention, dob: date) -> None:
        mention.dob = dob
        self.db.flush()

    def merge_features(self, mention: Mention, extra: dict) -> None:
        """REPLACE features with a merged fresh dict -- in-place mutation of
        a JSONB column is invisible to SQLAlchemy change tracking (the same
        rule repositories/processing.py documents for run counters)."""
        merged = dict(mention.features or {})
        merged.update(extra)
        mention.features = merged
        self.db.flush()

    def list_for_document(self, document_id: uuid.UUID) -> list[Mention]:
        return list(
            self.db.scalars(
                select(Mention)
                .where(Mention.document_id == document_id)
                .order_by(Mention.created_at)
            )
        )
