"""Exposure table exports (docs/plan.md §5: GET /exports/exposure.csv|.xlsx;
§2/§1's column groups). One row per person; four column groups, in the
plan's order:

  identity  -- person_id, best_name, aliases ("name [kind]; ..."), dob
  flags     -- one boolean column per §1 category
  evidence  -- document_count, mention_count, evidence_count
  quality   -- per-category confidence, er_confidence, review_status

Both formats render from the SAME row iterator so the CSV and the XLSX can
never disagree. CSV streams (a 1M-person export must not buffer in RAM);
XLSX is built with openpyxl's write_only mode for the same reason and
returned as bytes (the zip container cannot stream row-by-row anyway).
"""

from __future__ import annotations

import csv
import io
import uuid
from typing import Iterator

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import EXPOSURE_CATEGORIES, ExposureFlag, FlagEvidence, Person

_IDENTITY_COLUMNS = ("person_id", "best_name", "aliases", "dob")
_EVIDENCE_COLUMNS = ("document_count", "mention_count", "evidence_count")
_QUALITY_TAIL = ("er_confidence", "review_status")

EXPORT_COLUMNS: tuple[str, ...] = (
    *_IDENTITY_COLUMNS,
    *(f"flag_{category}" for category in EXPOSURE_CATEGORIES),
    *_EVIDENCE_COLUMNS,
    *(f"confidence_{category}" for category in EXPOSURE_CATEGORIES),
    *_QUALITY_TAIL,
)

_PAGE_SIZE = 500


def _format_aliases(aliases: list | None) -> str:
    if not aliases:
        return ""
    return "; ".join(f"{a.get('name')} [{a.get('kind')}]" for a in aliases)


def iter_export_rows(db: Session, run_id: uuid.UUID) -> Iterator[list]:
    """Yields the header row, then one list per person (keyset-free OFFSET
    paging in PK order -- stable because exports run against a finished
    run, not a moving one)."""
    yield list(EXPORT_COLUMNS)
    offset = 0
    while True:
        persons = list(
            db.scalars(
                select(Person)
                .where(Person.run_id == run_id, Person.mention_count > 0)
                .order_by(Person.best_name.asc(), Person.id.asc())
                .limit(_PAGE_SIZE)
                .offset(offset)
            )
        )
        if not persons:
            return
        person_ids = [p.id for p in persons]
        flags = db.scalars(
            select(ExposureFlag).where(ExposureFlag.person_id.in_(person_ids))
        ).all()
        flags_by_person: dict[uuid.UUID, dict[str, ExposureFlag]] = {}
        for flag in flags:
            flags_by_person.setdefault(flag.person_id, {})[flag.category] = flag
        evidence_counts = dict(
            db.execute(
                select(ExposureFlag.person_id, func.count(FlagEvidence.id))
                .join(FlagEvidence, FlagEvidence.exposure_flag_id == ExposureFlag.id)
                .where(ExposureFlag.person_id.in_(person_ids))
                .group_by(ExposureFlag.person_id)
            ).all()
        )

        for person in persons:
            person_flags = flags_by_person.get(person.id, {})
            row: list = [
                str(person.id),
                person.best_name,
                _format_aliases(person.aliases),
                person.dob.isoformat() if person.dob else "",
            ]
            for category in EXPOSURE_CATEGORIES:
                flag = person_flags.get(category)
                row.append(bool(flag.exposed) if flag else False)
            row.extend(
                [
                    person.document_count,
                    person.mention_count,
                    evidence_counts.get(person.id, 0),
                ]
            )
            for category in EXPOSURE_CATEGORIES:
                flag = person_flags.get(category)
                row.append(
                    float(flag.confidence)
                    if flag is not None and flag.confidence is not None
                    else ""
                )
            row.extend(
                [
                    float(person.er_confidence) if person.er_confidence is not None else "",
                    person.review_status,
                ]
            )
            yield row
        offset += _PAGE_SIZE


def iter_exposure_csv(db: Session, run_id: uuid.UUID) -> Iterator[str]:
    """Streaming CSV chunks (one line per yield) for StreamingResponse."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in iter_export_rows(db, run_id):
        writer.writerow(row)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)


def build_exposure_xlsx(db: Session, run_id: uuid.UUID) -> bytes:
    """openpyxl write-only workbook -> bytes."""
    from openpyxl import Workbook

    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(title="exposure")
    for row in iter_export_rows(db, run_id):
        sheet.append(row)
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()
