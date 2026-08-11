"""Scenario objects (docs/plan.md §8, Decision D6): each scenario emits its
documents AND their manifest entries through one code path
(`BuildContext.emit`), so the answer key cannot drift from the corpus.

Digital-only today. The scanned/eml/png/problem-file scenarios plug in as
further Scenario subclasses appended to `build_scenarios` once their
renderers exist — no scenario here fakes or stubs them.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field

from faker import Faker

from corpusgen import identities as ident_mod
from corpusgen import templates
from corpusgen.config import CorpusConfig
from corpusgen.identities import Identity, IdentityPool
from corpusgen.manifest import ManifestDocument
from corpusgen.renderers import EXTENSION, FILE_CLASS, RENDERERS, DocumentSpec, Plant
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
    _seq: int = 0

    def doc_date(self) -> dt.date:
        # Fixed window (2025-01-01 .. 2026-06-30) — no wall clock anywhere.
        return dt.date(2025, 1, 1) + dt.timedelta(days=self.rng.randrange(0, 546))

    def emit(self, spec: DocumentSpec, renderer: str, tags: list[str]) -> ManifestDocument:
        self._seq += 1
        doc_uid = f"D{self._seq:04d}"
        filename = f"{doc_uid}_{spec.archetype}{EXTENSION[renderer]}"
        plantings = RENDERERS[renderer](spec, self.out_dir / filename)
        for planting in plantings:
            if planting["person_uid"]:
                self.covered.add(planting["person_uid"])
        doc = ManifestDocument(
            doc_uid=doc_uid,
            filename=filename,
            file_class=FILE_CLASS[renderer],
            renderer=renderer,
            scenario_tags=tags,
            problem=None,
            plantings=plantings,
        )
        self.documents.append(doc)
        return doc


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
    evil-twin PNG of this sheet is tomorrow's scenario."""

    name = "bulk_spreadsheet"

    def emit(self, ctx: BuildContext) -> None:
        uncovered = [p for p in ctx.pool.identities if p.person_uid not in ctx.covered]
        persons = ctx.rng.sample(uncovered, ctx.cfg.bulk_spreadsheet_rows)
        rows = [
            {
                "Name": (p.person_uid, p.canonical_name),
                "SSN": (p.person_uid, p.elements["ssn"]),
                "DOB": (p.person_uid, p.dob),
                "Email": (p.person_uid, p.elements["email"]),
                "Phone": (p.person_uid, p.elements["phone"]),
                "Account": (p.person_uid, p.elements["financial_account"]),
            }
            for p in persons
        ]
        spec = templates.customer_export(ctx.doc_date(), rows)
        ctx.emit(spec, "xlsx", [self.name])


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
            rows = [
                {
                    "Name": (p.person_uid, p.canonical_name),
                    "SSN": (p.person_uid, p.elements["ssn"]),
                    "DOB": (p.person_uid, p.dob),
                    "Email": (p.person_uid, p.elements["email"]),
                    "Phone": (p.person_uid, p.elements["phone"]),
                    "Account": (p.person_uid, p.elements["financial_account"]),
                }
                for p in persons
            ]
            spec = templates.customer_export(ctx.doc_date(), rows)
            ctx.emit(spec, rng.choice(["csv", "xlsx"]), [self.name])
            return
        if rng.random() < 0.1 and person.variants:
            display = rng.choice(person.variants).value
        else:
            display = person.canonical_name
        _prose_doc(ctx, person, display, _pick_elements(rng), [self.name])


def build_scenarios(cfg: CorpusConfig) -> list[Scenario]:
    return [
        NicknameCluster(),
        SharedName(),
        PartialIdentifiers(),
        BulkSpreadsheet(),
        FalsePositiveTraps(),
        BackgroundFiller(),
    ]
