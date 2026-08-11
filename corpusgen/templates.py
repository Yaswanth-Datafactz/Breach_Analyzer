"""Layout archetypes (docs/plan.md §8): HR memo, benefits enrollment
letter, incident report, medical claim note, invoice, customer-export
spreadsheet, cred-dump txt, HTML support-ticket export. At most 3
archetypes per renderer — scenarios vary CONTENT (person, name variant,
planted elements), never layout.

Every template receives the plants it must embed and returns a
DocumentSpec in which each plant is attached to the exact block containing
its value — the renderer turns that into a physical location. A plant that
a template failed to embed trips the renderer's assertion, so a document
can never silently omit a value the manifest claims it contains.

Signature blocks in ordinary docs carry staff NAME + TITLE only; full
signature blocks (with company email/phone) appear only where the
FalsePositiveTraps scenario RECORDS them as trap plantings — the manifest
stays a complete account of extractable contact strings.
"""

from __future__ import annotations

import datetime as dt
import random
import re
from dataclasses import dataclass

from faker import Faker

from corpusgen.renderers import (
    DocumentSpec,
    EmailAttachment,
    EmailSpec,
    Paragraph,
    Plant,
    Table,
)

COMPANY = "Meridian Benefits Group"
COMPANY_DOMAIN = "meridianbenefits.example"

_CLIENT_COMPANIES = [
    "Acme Industrial Supply",
    "Bluepine Logistics LLC",
    "Harborview Dental Partners",
    "Stonegate Property Co.",
    "Lakeshore Catering Group",
]

_STAFF_TITLES = [
    "HR Benefits Coordinator",
    "Payroll Specialist",
    "HR Business Partner",
    "Claims Processor",
    "Customer Support Agent",
    "Compliance Analyst",
    "Enrollment Advisor",
    "Records Administrator",
]


@dataclass(frozen=True)
class Staff:
    name: str
    title: str
    email: str
    phone: str


def make_staff(rng: random.Random, faker: Faker) -> list[Staff]:
    staff = []
    for title in _STAFF_TITLES:
        first, last = faker.first_name(), faker.last_name()
        slug = re.sub(r"[^a-z0-9.]", "", f"{first}.{last}".lower())
        staff.append(
            Staff(
                name=f"{first} {last}",
                title=title,
                email=f"{slug}@{COMPANY_DOMAIN}",
                phone=f"(312) 555-{rng.randint(100, 199):04d}",
            )
        )
    return staff


# One sentence pool per plantable element type; {n} = display name,
# {v} = planted value. The value always appears verbatim and unbroken.
_SENTENCES: dict[str, list[str]] = {
    "ssn": [
        "Payroll currently lists the Social Security number {v} for {n}.",
        "Please confirm that SSN {v} on file is correct before the enrollment deadline.",
        "The corrected W-2 was reissued under SSN {v}.",
    ],
    "ssn_last4": [
        "For verification we matched the Social Security number on file ending in {v}.",
        "The SSN ending {v} was used to confirm the account holder.",
    ],
    "dob": [
        "Our enrollment record shows a date of birth of {v}.",
        "The dependent verification form lists {v} as the date of birth.",
    ],
    "phone": [
        "The daytime contact number on file is {v}.",
        "Please call {n} at {v} to confirm the change.",
    ],
    "email": [
        "Confirmation was sent to {v}.",
        "The preferred contact address on record is {v}.",
    ],
    "address": [
        "All correspondence should be mailed to {v}.",
        "The mailing address on file is {v}.",
    ],
    "drivers_license": [
        "Identity was verified against driver's license {v}.",
        "The license number {v} was photocopied for the I-9 file.",
    ],
    "passport": [
        "Passport number {v} was provided for the travel reimbursement.",
        "The identity document on file is passport {v}.",
    ],
    "financial_account": [
        "Direct deposit is routed to account {v}.",
        "The reimbursement was issued to account number {v}.",
    ],
    "credit_card": [
        "The corporate card on file, {v}, was charged for the copay.",
        "Card number {v} was used for the premium payment.",
    ],
    "medical": [
        "The claim references treatment for {v}.",
        "Coverage was continued for the diagnosis of {v}.",
    ],
    "employee_id": [
        "The record is filed under employee ID {v}.",
        "Reference employee ID {v} on all follow-up correspondence.",
    ],
}


def _body_paragraphs(rng: random.Random, name: str, plants: list[Plant]) -> list[Paragraph]:
    """One short paragraph per planted element, sentence chosen by rng."""
    paragraphs = []
    for plant in plants:
        sentence = rng.choice(_SENTENCES[plant.element_type]).format(n=name, v=plant.value)
        paragraphs.append(Paragraph(sentence, plants=[plant]))
    return paragraphs


def _split_name_plant(plants: list[Plant]) -> tuple[Plant | None, list[Plant]]:
    name_plant = next((p for p in plants if p.element_type == "name"), None)
    rest = [p for p in plants if p.element_type != "name"]
    return name_plant, rest


# --- archetype: HR memo (pdf / docx / txt) ---------------------------------


def hr_memo(
    rng: random.Random,
    staff: list[Staff],
    doc_date: dt.date,
    subject_name: str,
    plants: list[Plant],
) -> DocumentSpec:
    name_plant, element_plants = _split_name_plant(plants)
    author = rng.choice(staff)
    re_line = rng.choice(
        ["Payroll record correction", "Benefits enrollment follow-up",
         "Personnel file update", "Direct deposit verification"]
    )
    header = Paragraph(
        f"To: {subject_name}\nFrom: {author.name}, {author.title}\n"
        f"Date: {doc_date.isoformat()}\nRe: {re_line}",
        plants=[name_plant] if name_plant else [],
    )
    blocks = [header]
    blocks.extend(_body_paragraphs(rng, subject_name, element_plants))
    blocks.append(
        Paragraph(
            "Please review the above and report any discrepancy to Human Resources "
            f"within ten business days.\nRegards,\n{author.name}\n{author.title}, {COMPANY}"
        )
    )
    return DocumentSpec("hr_memo", f"{COMPANY} — Internal Memorandum", doc_date, blocks)


# --- archetype: benefits enrollment letter (pdf) ---------------------------


def benefits_letter(
    rng: random.Random,
    staff: list[Staff],
    doc_date: dt.date,
    subject_name: str,
    plants: list[Plant],
) -> DocumentSpec:
    name_plant, element_plants = _split_name_plant(plants)
    author = rng.choice(staff)
    plan_name = rng.choice(["Choice Plus PPO", "Essential HMO", "HighSaver HDHP"])
    salutation = Paragraph(
        f"Dear {subject_name},", plants=[name_plant] if name_plant else []
    )
    blocks = [
        Paragraph(f"{COMPANY}\nEnrollment Services\n{doc_date.isoformat()}"),
        salutation,
        Paragraph(
            f"Your {plan_name} enrollment for the upcoming plan year has been processed. "
            "The details we hold on file are summarized below; contact Enrollment Services "
            "if any item is out of date."
        ),
    ]
    blocks.extend(_body_paragraphs(rng, subject_name, element_plants))
    blocks.append(Paragraph(f"Sincerely,\n{author.name}\n{author.title}, {COMPANY}"))
    return DocumentSpec("benefits_letter", f"{COMPANY} — Enrollment Confirmation", doc_date, blocks)


# --- archetype: incident report (pdf / docx) -------------------------------


def incident_report(
    rng: random.Random,
    staff: list[Staff],
    doc_date: dt.date,
    subject_name: str,
    plants: list[Plant],
) -> DocumentSpec:
    name_plant, element_plants = _split_name_plant(plants)
    reporter = rng.choice(staff)
    incident = rng.choice(
        ["misdirected mail", "records access request", "duplicate enrollment entry",
         "returned direct deposit"]
    )
    blocks = [
        Paragraph(
            f"1. Summary\nOn {doc_date.isoformat()} the records team logged an incident "
            f"involving {incident}. This report documents the affected record."
        ),
        Paragraph(
            f"2. Affected individual\nName on record: {subject_name}",
            plants=[name_plant] if name_plant else [],
        ),
        Paragraph("3. Data elements referenced"),
    ]
    blocks.extend(_body_paragraphs(rng, subject_name, element_plants))
    blocks.append(
        Paragraph(
            f"4. Disposition\nNo further action required at this time.\n"
            f"Reported by: {reporter.name}, {reporter.title}"
        )
    )
    return DocumentSpec("incident_report", f"{COMPANY} — Incident Report", doc_date, blocks)


# --- archetype: medical claim note (docx) ----------------------------------

_CLAIM_LABELS: dict[str, str] = {
    "name": "Member",
    "dob": "Date of Birth",
    "medical": "Diagnosis",
    "ssn": "Member SSN",
    "ssn_last4": "SSN (last 4)",
    "financial_account": "Account",
    "employee_id": "Member ID",
    "email": "Email",
    "phone": "Phone",
    "address": "Address",
    "credit_card": "Card on File",
    "drivers_license": "Driver's License",
    "passport": "Passport",
}


def medical_claim(
    rng: random.Random,
    staff: list[Staff],
    doc_date: dt.date,
    subject_name: str,
    plants: list[Plant],
) -> DocumentSpec:
    reviewer = rng.choice(staff)
    claim_no = f"CLM-{doc_date.year}-{rng.randint(10000, 99999)}"
    rows: list[list[str]] = [
        ["Claim #", claim_no],
        ["Date of Service", doc_date.isoformat()],
    ]
    cell_plants: list[tuple[int, int, Plant]] = []
    for plant in plants:
        plant.presentation = "table"
        rows.append([_CLAIM_LABELS[plant.element_type], plant.value])
        cell_plants.append((len(rows) - 1, 1, plant))
    rows.append(["Status", rng.choice(["Approved", "Pending Review", "Denied"])])
    blocks = [
        Table("Claim", ["Field", "Value"], rows, cell_plants),
        Paragraph(
            f"Adjudication note: claim {claim_no} processed under the standard schedule. "
            f"Reviewed by {reviewer.name}, {reviewer.title}."
        ),
    ]
    return DocumentSpec("medical_claim", f"{COMPANY} — Claim Adjudication Note", doc_date, blocks)


# --- archetype: invoice (xlsx) ---------------------------------------------


def invoice_sheet(
    rng: random.Random,
    doc_date: dt.date,
    order_row_value: str | None = None,
    order_plant: Plant | None = None,
) -> DocumentSpec:
    """Company-billed invoice — carries no person PII unless the trap
    scenario passes an Order # plant (an SSN-formatted order number sitting
    next to explicit 'Order #' context)."""
    inv_no = f"INV-{doc_date.year}-{rng.randint(10000, 99999)}"
    order_no = order_row_value or f"ORD-{rng.randint(100000, 999999)}"
    amount = rng.randint(1200, 98000) / 100 * 10
    rows = [
        ["Invoice #", inv_no],
        ["Invoice Date", doc_date.isoformat()],
        ["Order #", order_no],
        ["Bill To", rng.choice(_CLIENT_COMPANIES)],
        ["Payment Terms", "Net 30"],
        ["Amount Due", f"${amount:,.2f}"],
    ]
    cell_plants: list[tuple[int, int, Plant]] = []
    if order_plant is not None:
        order_plant.presentation = "table"
        cell_plants.append((2, 1, order_plant))
    items = Table(
        "Line Items",
        ["Description", "Qty", "Unit Price", "Amount"],
        [
            [rng.choice(["Enrollment processing", "Records audit", "COBRA administration",
                         "Open-enrollment support", "Claims batch review"]),
             str(rng.randint(1, 12)), f"${rng.randint(80, 400)}.00", f"${rng.randint(400, 4000)}.00"]
            for _ in range(rng.randint(2, 4))
        ],
    )
    return DocumentSpec(
        "invoice",
        f"{COMPANY} — Invoice",
        doc_date,
        [Table("Invoice", ["Field", "Value"], rows, cell_plants), items],
    )


# --- archetype: customer-export spreadsheet (xlsx / csv) -------------------

EXPORT_COLUMNS = ["Name", "SSN", "DOB", "Email", "Phone", "Account"]
_EXPORT_ELEMENT_BY_COL = {
    "Name": "name", "SSN": "ssn", "DOB": "dob", "Email": "email",
    "Phone": "phone", "Account": "financial_account",
}


def customer_export(
    doc_date: dt.date,
    rows: list[dict[str, tuple[str, str]]],
    columns: list[str] | None = None,
) -> DocumentSpec:
    """rows: one dict per person, column name -> (person_uid, value).
    Every populated cell becomes a table planting."""
    columns = columns or EXPORT_COLUMNS
    table_rows: list[list[str]] = []
    cell_plants: list[tuple[int, int, Plant]] = []
    for row in rows:
        values = []
        for c, col in enumerate(columns):
            uid, value = row[col]
            values.append(value)
            cell_plants.append(
                (len(table_rows), c,
                 Plant(uid, _EXPORT_ELEMENT_BY_COL[col], value, presentation="table"))
            )
        table_rows.append(values)
    return DocumentSpec(
        "customer_export",
        f"{COMPANY} — Customer Export",
        doc_date,
        [Table("Customers", columns, table_rows, cell_plants)],
    )


# --- archetype: cred-dump txt ----------------------------------------------


def cred_dump(
    doc_date: dt.date,
    entries: list[dict[str, tuple[str | None, str]]],
) -> DocumentSpec:
    """entries: per row, field -> (person_uid, value) for employee_id /
    username / password / ssn. Deliberately name-free — the
    PartialIdentifiers join runs through employee_id."""
    headers = ["employee_id", "username", "password", "ssn"]
    rows: list[list[str]] = []
    cell_plants: list[tuple[int, int, Plant]] = []
    for entry in entries:
        values = []
        for c, field_name in enumerate(headers):
            uid, value = entry[field_name]
            values.append(value)
            element_type = "credential" if field_name in ("username", "password") else field_name
            cell_plants.append(
                (len(rows), c, Plant(uid, element_type, value, presentation="table"))
            )
        rows.append(values)
    banner = Paragraph(
        "# meridian internal extract — DO NOT DISTRIBUTE\n"
        f"# pulled {doc_date.isoformat()} from hr-auth replica"
    )
    return DocumentSpec(
        "cred_dump",
        "hr-auth extract",
        doc_date,
        [banner, Table("creds", headers, rows, cell_plants)],
    )


# --- archetype: HTML support-ticket export ---------------------------------


def support_ticket(
    rng: random.Random,
    staff: list[Staff],
    doc_date: dt.date,
    subject_name: str,
    plants: list[Plant],
) -> DocumentSpec:
    name_plant, element_plants = _split_name_plant(plants)
    agent = rng.choice(staff)
    ticket_no = f"TCK-{rng.randint(100000, 999999)}"
    topic = rng.choice(
        ["ID card reissue", "beneficiary change", "premium billing question",
         "portal password reset", "coverage verification"]
    )
    rows: list[list[str]] = [
        ["Ticket #", ticket_no],
        ["Opened", doc_date.isoformat()],
        ["Topic", topic],
    ]
    cell_plants: list[tuple[int, int, Plant]] = []
    if name_plant is not None:
        name_plant.presentation = "table"
        rows.append(["Customer", name_plant.value])
        cell_plants.append((len(rows) - 1, 1, name_plant))
    for plant in element_plants:
        plant.presentation = "table"
        rows.append([_CLAIM_LABELS[plant.element_type], plant.value])
        cell_plants.append((len(rows) - 1, 1, plant))
    blocks = [
        Table("Ticket", ["Field", "Value"], rows, cell_plants),
        Paragraph(
            f"Customer contacted support regarding {topic}. Identity confirmed against the "
            "details above; the request was completed on the same call."
        ),
        Paragraph(f"Handled by {agent.name} — Customer Support, {COMPANY}."),
    ]
    return DocumentSpec("support_ticket", f"{COMPANY} — Support Ticket Export", doc_date, blocks)


# --- archetype: internal email thread (eml) --------------------------------

_EMAIL_SUBJECTS = [
    "Payroll record correction",
    "Benefits enrollment follow-up",
    "Dependent verification paperwork",
    "Returned direct deposit — action needed",
    "Records request from claims",
]


def email_message(
    rng: random.Random,
    staff: list[Staff],
    doc_date: dt.date,
    subject_name: str,
    plants: list[Plant],
    attachments: list[EmailAttachment],
    signature_contact: bool = False,
) -> EmailSpec:
    """Internal staff-to-staff email ABOUT the subject person. When
    signature_contact is set, the sender's full signature block (company
    email + phone) closes the body — the caller records those as trap
    plantings, keeping the manifest a complete account of extractable
    contact strings."""
    name_plant, element_plants = _split_name_plant(plants)
    sender, recipient = rng.sample(staff, 2)
    subject = rng.choice(_EMAIL_SUBJECTS)
    blocks = [
        Paragraph(f"Hi {recipient.name.split()[0]},"),
        Paragraph(
            f"Following up on the record for {subject_name} — details below.",
            plants=[name_plant] if name_plant else [],
        ),
    ]
    blocks.extend(_body_paragraphs(rng, subject_name, element_plants))
    if attachments:
        listed = ", ".join(a.filename for a in attachments)
        blocks.append(Paragraph(f"Attached for reference: {listed}."))
    if signature_contact:
        sig_email = Plant(None, "trap_signature_email", sender.email, presentation="signature")
        sig_phone = Plant(None, "trap_signature_phone", sender.phone, presentation="signature")
        blocks.append(
            Paragraph(
                f"Thanks,\n{sender.name}\n{sender.title}, {COMPANY}\n"
                f"{sender.email}\n{sender.phone}",
                plants=[sig_email, sig_phone],
            )
        )
    else:
        blocks.append(Paragraph(f"Thanks,\n{sender.name}\n{sender.title}, {COMPANY}"))
    return EmailSpec(
        archetype="email",
        title=subject,
        doc_date=doc_date,
        blocks=blocks,
        from_name=sender.name,
        from_email=sender.email,
        to_name=recipient.name,
        to_email=recipient.email,
        attachments=attachments,
    )


# --- FalsePositiveTraps content builders -----------------------------------
# These reuse the memo / report / ticket LAYOUTS above (still ≤3 archetypes
# per renderer) — only the content is trap material.


def trap_card_memo(
    rng: random.Random, staff: list[Staff], doc_date: dt.date, card_plant: Plant
) -> DocumentSpec:
    author = rng.choice(staff)
    blocks = [
        Paragraph(
            f"To: Accounts Payable\nFrom: {author.name}, {author.title}\n"
            f"Date: {doc_date.isoformat()}\nRe: Rejected reimbursement payment"
        ),
        Paragraph(
            f"The card number {card_plant.value} submitted with the reimbursement request "
            "failed validation (invalid check digit) and was not charged. Please resubmit "
            "the form with a valid payment card.",
            plants=[card_plant],
        ),
        Paragraph(f"Regards,\n{author.name}\n{author.title}, {COMPANY}"),
    ]
    return DocumentSpec("hr_memo", f"{COMPANY} — Internal Memorandum", doc_date, blocks)


def trap_placeholder_memo(
    rng: random.Random, staff: list[Staff], doc_date: dt.date, plants: list[Plant]
) -> DocumentSpec:
    """Template-instruction memo: {{ssn}} and XXX-XX-1234 are placeholders,
    not anyone's data."""
    author = rng.choice(staff)
    fmt_plant = next(p for p in plants if p.value == "XXX-XX-1234")
    tpl_plant = next(p for p in plants if p.value == "{{ssn}}")
    blocks = [
        Paragraph(
            f"To: Enrollment team\nFrom: {author.name}, {author.title}\n"
            f"Date: {doc_date.isoformat()}\nRe: Letter template usage"
        ),
        Paragraph(
            "When completing the enrollment letter, enter the Social Security number "
            f"in the format {fmt_plant.value}.",
            plants=[fmt_plant],
        ),
        Paragraph(
            f"The template field {tpl_plant.value} must be replaced with the member's "
            "actual value before mailing; letters containing the raw placeholder must "
            "not be sent.",
            plants=[tpl_plant],
        ),
        Paragraph(f"Regards,\n{author.name}\n{author.title}, {COMPANY}"),
    ]
    return DocumentSpec("hr_memo", f"{COMPANY} — Internal Memorandum", doc_date, blocks)


def trap_test_ticket(
    rng: random.Random, staff: list[Staff], doc_date: dt.date, ssn_plant: Plant
) -> DocumentSpec:
    agent = rng.choice(staff)
    ssn_plant.presentation = "table"
    rows = [
        ["Ticket #", f"TCK-{rng.randint(100000, 999999)}"],
        ["Opened", doc_date.isoformat()],
        ["Status", "TEST RECORD — DO NOT PROCESS"],
        ["Customer", "SAMPLE — Test User"],
        ["Member SSN", ssn_plant.value],
    ]
    blocks = [
        Table("Ticket", ["Field", "Value"], rows, [(4, 1, ssn_plant)]),
        Paragraph(
            "This ticket was created by the QA automation suite to verify the export "
            "pipeline. All values are sample data."
        ),
        Paragraph(f"Handled by {agent.name} — Customer Support, {COMPANY}."),
    ]
    return DocumentSpec("support_ticket", f"{COMPANY} — Support Ticket Export", doc_date, blocks)


def trap_signature_report(
    rng: random.Random,
    author: Staff,
    doc_date: dt.date,
    email_plant: Plant,
    phone_plant: Plant,
) -> DocumentSpec:
    """Routine ops report whose only contact strings are the STAFF signature
    block — company contacts, not breached-person PII."""
    system = rng.choice(
        ["enrollment portal", "claims intake queue", "document archive", "payroll interface"]
    )
    blocks = [
        Paragraph(
            f"1. Summary\nScheduled maintenance of the {system} completed on "
            f"{doc_date.isoformat()}. No member records were modified."
        ),
        Paragraph(
            "2. Verification\nPost-maintenance checks passed; audit logging confirmed "
            "for all service accounts."
        ),
        Paragraph(
            f"Reported by: {author.name}\n{author.title}, {COMPANY}\n"
            f"{author.email}\n{author.phone}",
            plants=[email_plant, phone_plant],
        ),
    ]
    return DocumentSpec("incident_report", f"{COMPANY} — Incident Report", doc_date, blocks)
