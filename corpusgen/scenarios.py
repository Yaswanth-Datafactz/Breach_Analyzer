"""Scenario objects (docs/plan.md §8, Decision D6): each scenario emits its
documents AND their manifest entries through one code path
(`BuildContext.emit`, or `allocate`+`register` for problem files whose
wrong extension IS the fixture), so the answer key cannot drift from the
corpus.

Ordering contract: BackgroundFiller fills the DIGITAL classes up to
cfg.target_docs (after the digital scenarios, so coverage and the bulk
sheet's uncovered-persons draw stay correct); the scanned/eml/png/problem
scenarios emit after it, on top of that fill — cfg.total_docs is the
whole-corpus size validate.py enforces.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import random
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from faker import Faker

from corpusgen import identities as ident_mod
from corpusgen import templates
from corpusgen.config import CorpusConfig
from corpusgen.identities import Identity, IdentityPool
from corpusgen.manifest import ManifestDocument
from corpusgen.renderers import (
    EXTENSION,
    FILE_CLASS,
    RENDERERS,
    DocumentSpec,
    EmailAttachment,
    Plant,
    problem_files,
)
from corpusgen.renderers.eml import normalize_zip_bytes
from corpusgen.templates import Staff

# Digital renderer/template pairings for single-subject prose docs.
# medical_claim and support_ticket place values in tables, the rest in prose.
PROSE_COMBOS: list[tuple[str, object]] = [
    ("digital_pdf", templates.hr_memo),
    ("digital_pdf", templates.benefits_letter),
    ("digital_pdf", templates.incident_report),
    ("docx", templates.hr_memo),
    ("docx", templates.medical_claim),
    ("docx", templates.incident_report),
    ("txt", templates.hr_memo),
    ("html", templates.support_ticket),
]

STRONG_ELEMENTS = ["ssn", "email", "phone"]
EXTRA_ELEMENTS = [
    "dob", "address", "drivers_license", "passport",
    "financial_account", "credit_card", "medical",
]

TRAP_KINDS = [
    "trap_order_number",
    "trap_card_invalid",
    "trap_test_ssn",
    "trap_placeholder",
    "trap_signature_email",
]


@dataclass
class BuildContext:
    rng: random.Random
    faker: Faker
    cfg: CorpusConfig
    pool: IdentityPool
    staff: list[Staff]
    out_dir: object  # pathlib.Path
    documents: list[ManifestDocument] = field(default_factory=list)
    covered: set[str] = field(default_factory=set)
    # BulkSpreadsheet leaves its sheet content here so PngScreenshots can
    # re-render the SAME rows as the evil-twin image.
    bulk_rows: list[dict] | None = None
    bulk_date: dt.date | None = None
    _seq: int = 0

    def doc_date(self) -> dt.date:
        # Fixed window (2025-01-01 .. 2026-06-30) — no wall clock anywhere.
        return dt.date(2025, 1, 1) + dt.timedelta(days=self.rng.randrange(0, 546))

    def allocate(self, archetype: str, ext: str) -> tuple[str, str]:
        """Claim the next doc_uid and its filename (ext chosen by the
        caller — problem files deliberately lie about it)."""
        self._seq += 1
        doc_uid = f"D{self._seq:04d}"
        return doc_uid, f"{doc_uid}_{archetype}{ext}"

    def register(
        self,
        doc_uid: str,
        filename: str,
        file_class: str,
        renderer: str,
        tags: list[str],
        plantings: list[dict],
        problem: dict | None = None,
        attachments: list[dict] | None = None,
    ) -> ManifestDocument:
        for planting in plantings:
            if planting["person_uid"]:
                self.covered.add(planting["person_uid"])
        doc = ManifestDocument(
            doc_uid=doc_uid,
            filename=filename,
            file_class=file_class,
            renderer=renderer,
            scenario_tags=tags,
            problem=problem,
            plantings=plantings,
            attachments=attachments,
        )
        self.documents.append(doc)
        return doc

    def emit(
        self,
        spec: DocumentSpec,
        renderer: str,
        tags: list[str],
        attachments: list[dict] | None = None,
    ) -> ManifestDocument:
        doc_uid, filename = self.allocate(spec.archetype, EXTENSION[renderer])
        plantings = RENDERERS[renderer](spec, self.out_dir / filename)
        return self.register(
            doc_uid, filename, FILE_CLASS[renderer], renderer, tags, plantings,
            attachments=attachments,
        )


def _plant(person: Identity, key: str) -> Plant:
    if key == "ssn_last4":
        return Plant(person.person_uid, "ssn_last4", person.elements["ssn"][-4:])
    if key == "dob":
        return Plant(person.person_uid, "dob", person.dob)
    element_type = "credential" if key in ("username", "password") else key
    return Plant(person.person_uid, element_type, person.elements[key])


def _display(person: Identity, kind: str) -> str:
    if kind == "canonical":
        return person.canonical_name
    return next(v.value for v in person.variants if v.kind == kind)


def _prose_doc(
    ctx: BuildContext,
    person: Identity,
    display_name: str,
    element_keys: list[str],
    tags: list[str],
    combo: tuple[str, object] | None = None,
) -> None:
    plants = [Plant(person.person_uid, "name", display_name)]
    plants.extend(_plant(person, key) for key in element_keys)
    renderer, template = combo or ctx.rng.choice(PROSE_COMBOS)
    spec = template(ctx.rng, ctx.staff, ctx.doc_date(), display_name, plants)
    ctx.emit(spec, renderer, tags)


def _pick_elements(rng: random.Random) -> list[str]:
    """1 strong identifier (keeps every doc ER-linkable) + 0-2 extras."""
    keys = [rng.choice(STRONG_ELEMENTS)]
    keys.extend(rng.sample(EXTRA_ELEMENTS, rng.randint(0, 2)))
    return keys


def _subject_plants(person: Identity, element_keys: list[str]) -> list[Plant]:
    plants = [Plant(person.person_uid, "name", person.canonical_name)]
    plants.extend(_plant(person, key) for key in element_keys)
    return plants


def _export_row(person: Identity) -> dict[str, tuple[str, str]]:
    return {
        "Name": (person.person_uid, person.canonical_name),
        "SSN": (person.person_uid, person.elements["ssn"]),
        "DOB": (person.person_uid, person.dob),
        "Email": (person.person_uid, person.elements["email"]),
        "Phone": (person.person_uid, person.elements["phone"]),
        "Account": (person.person_uid, person.elements["financial_account"]),
    }


class Scenario:
    name: str

    def emit(self, ctx: BuildContext) -> None:
        raise NotImplementedError


class NicknameCluster(Scenario):
    """≥N persons × 3-5 docs each, cycling name-variant kinds (canonical,
    nickname, then a sample of maiden/initials/misspelling/order_variant).
    Doc 1 always pairs the canonical name with SSN + email so the cluster
    has a deterministic ER spine."""

    name = "nickname_cluster"

    def emit(self, ctx: BuildContext) -> None:
        persons = [
            ctx.pool.by_uid[uid]
            for uid in ctx.pool.nickname_person_uids[: ctx.cfg.nickname_cluster_persons]
        ]
        for person in persons:
            n_docs = ctx.rng.randint(ctx.cfg.nickname_docs_min, ctx.cfg.nickname_docs_max)
            kinds = ["canonical", "nickname"] + ctx.rng.sample(
                ["maiden", "initials", "misspelling", "order_variant"], n_docs - 2
            )
            for i, kind in enumerate(kinds):
                if i == 0:
                    element_keys = ["ssn", "email"]
                else:
                    element_keys = _pick_elements(ctx.rng)
                _prose_doc(ctx, person, _display(person, kind), element_keys, [self.name])


class SharedName(Scenario):
    """Pairs of DIFFERENT people sharing a full name. Each member gets two
    docs under the identical canonical name, distinguished only by their
    own strong identifiers (DOB+SSN, then address+phone) — the must-NOT-
    merge case."""

    name = "shared_name"

    def emit(self, ctx: BuildContext) -> None:
        for uid_a, uid_b in ctx.pool.shared_name_pairs:
            for uid in (uid_a, uid_b):
                person = ctx.pool.by_uid[uid]
                _prose_doc(ctx, person, person.canonical_name, ["dob", "ssn"], [self.name])
                _prose_doc(ctx, person, person.canonical_name, ["address", "phone"], [self.name])


class PartialIdentifiers(Scenario):
    """SSN alone in doc A (name-free cred dump keyed by employee_id), name
    in doc B joined only via employee_id + email, with a last-4 SSN
    reference in the name-bearing doc."""

    name = "partial_identifiers"

    def emit(self, ctx: BuildContext) -> None:
        start = 2 * ctx.cfg.shared_name_pairs + ctx.cfg.nickname_cluster_persons
        persons = ctx.pool.identities[start : start + ctx.cfg.partial_identifier_persons]

        chunk = ctx.cfg.partial_dump_chunk
        for i in range(0, len(persons), chunk):
            entries = [
                {
                    "employee_id": (p.person_uid, p.elements["employee_id"]),
                    "username": (p.person_uid, p.elements["username"]),
                    "password": (p.person_uid, p.elements["password"]),
                    "ssn": (p.person_uid, p.elements["ssn"]),
                }
                for p in persons[i : i + chunk]
            ]
            spec = templates.cred_dump(ctx.doc_date(), entries)
            ctx.emit(spec, "txt", [self.name])

        for person in persons:
            _prose_doc(
                ctx,
                person,
                person.canonical_name,
                ["employee_id", "email", "ssn_last4"],
                [self.name],
            )


class BulkSpreadsheet(Scenario):
    """One xlsx exposing cfg.bulk_spreadsheet_rows persons as rows (name,
    SSN, DOB, email, phone, account) — every cell a recorded planting. The
    sheet content is parked on the context for PngScreenshots' evil twin."""

    name = "bulk_spreadsheet"

    def emit(self, ctx: BuildContext) -> None:
        uncovered = [p for p in ctx.pool.identities if p.person_uid not in ctx.covered]
        persons = ctx.rng.sample(uncovered, ctx.cfg.bulk_spreadsheet_rows)
        rows = [_export_row(p) for p in persons]
        date = ctx.doc_date()
        spec = templates.customer_export(date, rows)
        ctx.emit(spec, "xlsx", [self.name])
        ctx.bulk_rows = rows
        ctx.bulk_date = date


class FalsePositiveTraps(Scenario):
    """Trap docs that PII detectors must not fall for. Five kinds cycled:
    SSN-formatted order numbers beside 'Order #' context, Luhn-invalid
    card-likes, TEST/SAMPLE records, template placeholders ({{ssn}} and
    XXX-XX-1234), and staff signature blocks. Trap values are drawn through
    the pool's `used` registry, so they can never collide with a real
    identity's element (the collision validate.py enforces)."""

    name = "false_positive_traps"

    def emit(self, ctx: BuildContext) -> None:
        rng = ctx.rng
        for i in range(ctx.cfg.trap_docs):
            kind = TRAP_KINDS[i % len(TRAP_KINDS)]
            if kind == "trap_order_number":
                value = ident_mod.make_ssn(rng, ctx.pool.used["ssn"])
                plant = Plant(None, kind, value)
                spec = templates.invoice_sheet(
                    rng, ctx.doc_date(), order_row_value=value, order_plant=plant
                )
                ctx.emit(spec, "xlsx", [self.name])
            elif kind == "trap_card_invalid":
                value = ident_mod.make_luhn_invalid_card(rng, ctx.pool.used["credit_card"])
                spec = templates.trap_card_memo(rng, ctx.staff, ctx.doc_date(), Plant(None, kind, value))
                ctx.emit(spec, "digital_pdf", [self.name])
            elif kind == "trap_test_ssn":
                value = ident_mod.make_ssn(rng, ctx.pool.used["ssn"])
                spec = templates.trap_test_ticket(rng, ctx.staff, ctx.doc_date(), Plant(None, kind, value))
                ctx.emit(spec, "html", [self.name])
            elif kind == "trap_placeholder":
                plants = [
                    Plant(None, kind, "XXX-XX-1234"),
                    Plant(None, kind, "{{ssn}}"),
                ]
                spec = templates.trap_placeholder_memo(rng, ctx.staff, ctx.doc_date(), plants)
                ctx.emit(spec, "txt", [self.name])
            else:  # trap_signature_email
                staff = rng.choice(ctx.staff)
                email_plant = Plant(None, kind, staff.email, presentation="signature")
                phone_plant = Plant(None, "trap_signature_phone", staff.phone, presentation="signature")
                spec = templates.trap_signature_report(
                    rng, staff, ctx.doc_date(), email_plant, phone_plant
                )
                ctx.emit(spec, "docx", [self.name])


class BackgroundFiller(Scenario):
    """Routine docs: first one per still-uncovered identity (every identity
    appears somewhere), then round-robin filler until cfg.target_docs."""

    name = "background"

    def emit(self, ctx: BuildContext) -> None:
        for person in [p for p in ctx.pool.identities if p.person_uid not in ctx.covered]:
            self._one_doc(ctx, person)
        while len(ctx.documents) < ctx.cfg.target_docs:
            self._one_doc(ctx, ctx.rng.choice(ctx.pool.identities))

    def _one_doc(self, ctx: BuildContext, person: Identity) -> None:
        rng = ctx.rng
        if rng.random() < 0.15:
            # Small customer export (csv or xlsx) — person plus a few others.
            others = [p for p in ctx.pool.identities if p.person_uid != person.person_uid]
            persons = [person] + rng.sample(others, rng.randint(2, 4))
            spec = templates.customer_export(ctx.doc_date(), [_export_row(p) for p in persons])
            ctx.emit(spec, rng.choice(["csv", "xlsx"]), [self.name])
            return
        if rng.random() < 0.1 and person.variants:
            display = rng.choice(person.variants).value
        else:
            display = person.canonical_name
        _prose_doc(ctx, person, display, _pick_elements(rng), [self.name])


class ScannedBatch(Scenario):
    """cfg.scanned_docs prose docs through the scanned_pdf renderer
    (rasterize -> degrade -> image-only PDF): the OCR/vision path's real
    volume (~20% of the corpus). Same memo/letter/report archetypes as the
    digital PDFs; plantings carry presentation 'image'."""

    name = "scanned_batch"

    _TEMPLATES = [templates.hr_memo, templates.benefits_letter, templates.incident_report]

    def emit(self, ctx: BuildContext) -> None:
        rng = ctx.rng
        for _ in range(ctx.cfg.scanned_docs):
            person = rng.choice(ctx.pool.identities)
            if rng.random() < 0.1 and person.variants:
                display = rng.choice(person.variants).value
            else:
                display = person.canonical_name
            combo = ("scanned_pdf", rng.choice(self._TEMPLATES))
            _prose_doc(ctx, person, display, _pick_elements(rng), [self.name], combo=combo)


class EmailThreads(Scenario):
    """cfg.eml_docs staff-to-staff emails about a subject person. The first
    2*cfg.eml_shared_attachment_pairs emails attach IDENTICAL bytes in
    pairs (the sha256-dedup measurable, recorded per-doc in
    manifest.attachments); later emails cycle one-off docx/xlsx/pdf
    attachments; every fourth carries a full staff signature block,
    recorded as trap plantings."""

    name = "email_thread"

    def emit(self, ctx: BuildContext) -> None:
        rng = ctx.rng
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            shared: list[EmailAttachment] = []
            for pair_no in range(ctx.cfg.eml_shared_attachment_pairs):
                persons = rng.sample(ctx.pool.identities, rng.randint(3, 4))
                spec = templates.customer_export(
                    ctx.doc_date(), [_export_row(p) for p in persons]
                )
                shared.append(
                    self._attachment(
                        tmp_dir, f"quarterly_export_{pair_no + 1}.xlsx", "xlsx", spec
                    )
                )

            one_off_seq = 0
            for i in range(ctx.cfg.eml_docs):
                attachments: list[EmailAttachment] = []
                if i < 2 * len(shared):
                    attachments = [self._reattach(shared[i // 2])]
                elif (i - 2 * len(shared)) % 3 == 0:
                    one_off_seq += 1
                    attachments = [self._one_off(ctx, tmp_dir, one_off_seq)]
                person = rng.choice(ctx.pool.identities)
                plants = _subject_plants(person, _pick_elements(rng))
                spec = templates.email_message(
                    rng, ctx.staff, ctx.doc_date(), person.canonical_name,
                    plants, attachments, signature_contact=(i % 4 == 3),
                )
                meta = [
                    {
                        "filename": a.filename,
                        "sha256": hashlib.sha256(a.content).hexdigest(),
                        "byte_size": len(a.content),
                    }
                    for a in attachments
                ]
                ctx.emit(spec, "eml", [self.name], attachments=meta or None)

    def _one_off(self, ctx: BuildContext, tmp_dir: Path, seq: int) -> EmailAttachment:
        rng = ctx.rng
        # pdf first: the shared attachments already cover xlsx, so the mini
        # profile's two one-offs must land pdf+docx to cover all three.
        kind = ("pdf", "docx", "xlsx")[(seq - 1) % 3]
        person = rng.choice(ctx.pool.identities)
        if kind == "docx":
            spec = templates.medical_claim(
                rng, ctx.staff, ctx.doc_date(), person.canonical_name,
                _subject_plants(person, ["dob", "medical", "employee_id"]),
            )
            return self._attachment(tmp_dir, f"claim_note_{seq}.docx", "docx", spec)
        if kind == "xlsx":
            persons = rng.sample(ctx.pool.identities, rng.randint(3, 4))
            spec = templates.customer_export(
                ctx.doc_date(), [_export_row(p) for p in persons]
            )
            return self._attachment(tmp_dir, f"member_export_{seq}.xlsx", "xlsx", spec)
        spec = templates.benefits_letter(
            rng, ctx.staff, ctx.doc_date(), person.canonical_name,
            _subject_plants(person, _pick_elements(rng)),
        )
        return self._attachment(tmp_dir, f"enrollment_letter_{seq}.pdf", "digital_pdf", spec)

    _MIME = {
        "docx": ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document"),
        "xlsx": ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        "digital_pdf": ("application", "pdf"),
    }

    def _attachment(
        self, tmp_dir: Path, filename: str, renderer: str, spec: DocumentSpec
    ) -> EmailAttachment:
        inner_path = tmp_dir / filename
        inner_plantings = RENDERERS[renderer](spec, inner_path)
        content = inner_path.read_bytes()
        if renderer in ("docx", "xlsx"):
            content = normalize_zip_bytes(content)
        maintype, subtype = self._MIME[renderer]
        plantings = [
            {**p, "location": {"part": f"attachment:{filename}", **p["location"]}}
            for p in inner_plantings
        ]
        return EmailAttachment(filename, content, maintype, subtype, plantings)

    @staticmethod
    def _reattach(attachment: EmailAttachment) -> EmailAttachment:
        """Fresh planting dicts, IDENTICAL bytes — each manifest document
        owns its planting entries, but the sha256s must collide."""
        return EmailAttachment(
            attachment.filename,
            attachment.content,
            attachment.maintype,
            attachment.subtype,
            [{**p, "location": dict(p["location"])} for p in attachment.plantings],
        )


class PngScreenshots(Scenario):
    """Screenshot-style table PNGs. First the BulkSpreadsheet evil twin —
    the SAME sheet content re-rendered as an image (same rows, same date)
    — then small one-off exports up to cfg.png_docs."""

    name = "png_screenshot"

    def emit(self, ctx: BuildContext) -> None:
        rng = ctx.rng
        assert ctx.bulk_rows is not None and ctx.bulk_date is not None
        twin = templates.customer_export(ctx.bulk_date, ctx.bulk_rows)
        ctx.emit(twin, "png", ["bulk_spreadsheet", self.name])
        for _ in range(ctx.cfg.png_docs - 1):
            persons = rng.sample(ctx.pool.identities, rng.randint(3, 5))
            spec = templates.customer_export(
                ctx.doc_date(), [_export_row(p) for p in persons]
            )
            ctx.emit(spec, "png", [self.name])


class ProblemFiles(Scenario):
    """cfg.problem_sets of the six problem kinds
    (renderers/problem_files.py). Each manifest entry carries the problem
    contract the exception-investigator fixtures are built from; the
    recoverable kinds keep real plantings so recovery re-joins the answer
    key."""

    name = "problem_files"

    def emit(self, ctx: BuildContext) -> None:
        for _ in range(ctx.cfg.problem_sets):
            self._password(ctx)
            self._truncated(ctx)
            self._zero_byte(ctx)
            self._xlsx_as_pdf(ctx)
            self._docx_as_txt(ctx)
            self._png_as_xlsx(ctx)

    def _register(
        self,
        ctx: BuildContext,
        doc_uid: str,
        filename: str,
        file_class: str,
        result: tuple[list[dict], dict],
    ) -> None:
        plantings, problem = result
        ctx.register(
            doc_uid, filename, file_class, "problem_files", [self.name],
            plantings, problem=problem,
        )

    def _password(self, ctx: BuildContext) -> None:
        rng = ctx.rng
        person = rng.choice(ctx.pool.identities)
        spec = templates.benefits_letter(
            rng, ctx.staff, ctx.doc_date(), person.canonical_name,
            _subject_plants(person, ["ssn", "email"]),
        )
        password = f"mbg-{rng.randint(100000, 999999)}"
        doc_uid, filename = ctx.allocate(spec.archetype, ".pdf")
        result = problem_files.password_pdf(spec, ctx.out_dir / filename, password)
        self._register(ctx, doc_uid, filename, "pdf_digital", result)

    def _truncated(self, ctx: BuildContext) -> None:
        rng = ctx.rng
        person = rng.choice(ctx.pool.identities)
        spec = templates.hr_memo(
            rng, ctx.staff, ctx.doc_date(), person.canonical_name,
            _subject_plants(person, ["ssn"]),
        )
        doc_uid, filename = ctx.allocate(spec.archetype, ".pdf")
        result = problem_files.truncated_pdf(spec, ctx.out_dir / filename)
        self._register(ctx, doc_uid, filename, "pdf_digital", result)

    def _zero_byte(self, ctx: BuildContext) -> None:
        doc_uid, filename = ctx.allocate("scan", ".pdf")
        result = problem_files.zero_byte(ctx.out_dir / filename)
        self._register(ctx, doc_uid, filename, "unknown", result)

    def _xlsx_as_pdf(self, ctx: BuildContext) -> None:
        rng = ctx.rng
        persons = rng.sample(ctx.pool.identities, 3)
        spec = templates.customer_export(ctx.doc_date(), [_export_row(p) for p in persons])
        doc_uid, filename = ctx.allocate(spec.archetype, ".pdf")
        result = problem_files.xlsx_as_pdf(spec, ctx.out_dir / filename)
        self._register(ctx, doc_uid, filename, "xlsx", result)

    def _docx_as_txt(self, ctx: BuildContext) -> None:
        rng = ctx.rng
        person = rng.choice(ctx.pool.identities)
        spec = templates.hr_memo(
            rng, ctx.staff, ctx.doc_date(), person.canonical_name,
            _subject_plants(person, _pick_elements(rng)),
        )
        doc_uid, filename = ctx.allocate(spec.archetype, ".txt")
        result = problem_files.docx_as_txt(spec, ctx.out_dir / filename)
        self._register(ctx, doc_uid, filename, "docx", result)

    def _png_as_xlsx(self, ctx: BuildContext) -> None:
        rng = ctx.rng
        persons = rng.sample(ctx.pool.identities, 4)
        spec = templates.customer_export(ctx.doc_date(), [_export_row(p) for p in persons])
        doc_uid, filename = ctx.allocate(spec.archetype, ".xlsx")
        result = problem_files.png_as_xlsx(spec, ctx.out_dir / filename)
        self._register(ctx, doc_uid, filename, "png", result)


def build_scenarios(cfg: CorpusConfig) -> list[Scenario]:
    return [
        NicknameCluster(),
        SharedName(),
        PartialIdentifiers(),
        BulkSpreadsheet(),
        FalsePositiveTraps(),
        BackgroundFiller(),
        ScannedBatch(),
        EmailThreads(),
        PngScreenshots(),
        ProblemFiles(),
    ]
