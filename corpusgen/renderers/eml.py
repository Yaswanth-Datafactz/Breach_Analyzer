"""EML renderer (stdlib email): multipart RFC-5322 message — headers in
the corpus's fixed date window, text/plain body built from the spec's
Paragraph blocks, attachments carried as MIME parts from pre-rendered
renderer outputs (EmailAttachment bytes).

Locations are per part: body plantings get {"part": "body", "line": n}
(1-based over the decoded body text); attachment plantings arrive from
their own renderer already scoped to {"part": "attachment:<filename>",
...inner location}; the staff From/To addresses are themselves recorded
as trap plantings at {"part": "headers", "header": "From"|"To"} so the
manifest stays a complete account of extractable contact strings
(templates.py's signature-block rule, applied to envelopes).

Determinism: Date is doc_date at a fixed clock time, Message-ID is a CRC
of the envelope+body (make_msgid would inject wall-clock randomness), and
attachment bytes are already normalized by the scenario — the .eml is
byte-stable for a given seed.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import zipfile
import zlib
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

from corpusgen.renderers import (
    EmailSpec,
    Paragraph,
    Plant,
    planting_dict,
)

_TZ = dt.timezone(dt.timedelta(hours=-5))  # company HQ offset, fixed
_ZIP_DATE_TIME = (2026, 1, 15, 12, 0, 0)
_CORE_XML_STAMP = "2026-01-15T12:00:00Z"
_CORE_XML_TIME_RE = re.compile(
    rb"(<dcterms:(?:created|modified)[^>]*>)[^<]*(</dcterms:(?:created|modified)>)"
)


def normalize_zip_bytes(data: bytes) -> bytes:
    """Rewrite a zip container (docx/xlsx) with fixed entry timestamps AND
    a fixed docProps/core.xml created/modified stamp (openpyxl overwrites
    `modified` with the wall clock at save, ignoring pinned properties) so
    identical logical content yields identical bytes — the sha256-dedup
    ground truth depends on it."""
    with zipfile.ZipFile(io.BytesIO(data)) as src:
        out_buf = io.BytesIO()
        with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as out:
            for item in src.infolist():
                content = src.read(item.filename)
                if item.filename == "docProps/core.xml":
                    content = _CORE_XML_TIME_RE.sub(
                        rb"\g<1>" + _CORE_XML_STAMP.encode() + rb"\g<2>", content
                    )
                info = zipfile.ZipInfo(item.filename, date_time=_ZIP_DATE_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = item.external_attr
                out.writestr(info, content)
    return out_buf.getvalue()


def render(spec: EmailSpec, path: Path) -> list[dict]:
    assert isinstance(spec, EmailSpec), "eml renderer requires an EmailSpec"
    body_lines: list[str] = []
    plantings: list[dict] = []

    for block in spec.blocks:
        assert isinstance(block, Paragraph), "eml bodies are paragraph-only"
        block_lines = block.text.split("\n")
        start = len(body_lines)
        body_lines.extend(block_lines)
        for plant in block.plants:
            line_no = next(
                (
                    start + i + 1  # 1-based
                    for i, line in enumerate(block_lines)
                    if plant.value in line
                ),
                None,
            )
            assert line_no is not None, f"plant {plant.value!r} not in paragraph text"
            plantings.append(planting_dict(plant, {"part": "body", "line": line_no}))
        body_lines.append("")

    msg = EmailMessage(policy=SMTP)
    msg["From"] = f"{spec.from_name} <{spec.from_email}>"
    msg["To"] = f"{spec.to_name} <{spec.to_email}>"
    msg["Subject"] = spec.title
    msg["Date"] = dt.datetime.combine(spec.doc_date, dt.time(9, 30), tzinfo=_TZ)
    digest = zlib.crc32(
        "\x1f".join([spec.from_email, spec.to_email, spec.title, *body_lines]).encode()
    )
    msg["Message-ID"] = f"<{digest:08x}.{spec.doc_date:%Y%m%d}@meridianbenefits.example>"
    msg.set_content("\n".join(body_lines))

    for header in ("From", "To"):
        address = spec.from_email if header == "From" else spec.to_email
        plantings.append(
            planting_dict(
                Plant(None, "trap_staff_email", address, presentation="header"),
                {"part": "headers", "header": header},
            )
        )

    for attachment in spec.attachments:
        msg.add_attachment(
            attachment.content,
            maintype=attachment.maintype,
            subtype=attachment.subtype,
            filename=attachment.filename,
        )
        for plant in attachment.plantings:
            expected = f"attachment:{attachment.filename}"
            assert plant["location"]["part"] == expected, plant
        plantings.extend(attachment.plantings)

    path.write_bytes(msg.as_bytes())
    return plantings
