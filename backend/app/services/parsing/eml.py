"""EML parsing via the stdlib email package (docs/plan.md D1: everything we
run is OSS -- for RFC 822 the stdlib IS the reference implementation).

Body parts become `email_part` passages: {"part": "headers"} for the
From/To/Cc/Subject/Date block (names in address headers are real PII
signal) and {"part": "body", "index": n, "content_type": ...} for each
text part in walk order -- text/html bodies are tag-stripped through the
same extractor html files use, so one code path owns HTML-to-text.

Attachments are NOT parsed here. They are decoded and returned as
`ExtractedAttachment`s for the pipeline to re-ingest as child documents
(parent_document_id + source_kind='attachment', plan §3) -- that recursion
is what gives an attached xlsx the same classify/quarantine/dedup treatment
as a corpus file, including sha256 dedup when the identical attachment
rides on five emails (plan §9 rule 2).
"""

from __future__ import annotations

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

from app.services.parsing.html_txt import html_to_text
from app.services.parsing.types import ExtractedAttachment, ParsedPassage, ParseResult

_HEADER_FIELDS = ("From", "To", "Cc", "Subject", "Date")


def _attachment_bytes(part: EmailMessage) -> bytes:
    payload = part.get_payload(decode=True)
    if payload is not None:
        return payload
    # message/rfc822 attachments decode to None; their bytes are the
    # embedded message itself (re-ingested as an eml child document).
    embedded = part.get_payload()
    if isinstance(embedded, list) and embedded:
        return embedded[0].as_bytes()
    return b""


def parse_eml(content: bytes) -> ParseResult:
    message = BytesParser(policy=policy.default).parsebytes(content)

    passages: list[ParsedPassage] = []
    attachments: list[ExtractedAttachment] = []

    header_lines = [f"{name}: {message[name]}" for name in _HEADER_FIELDS if message[name]]
    if header_lines:
        passages.append(
            ParsedPassage(
                kind="email_part", locator={"part": "headers"}, text="\n".join(header_lines)
            )
        )

    body_index = 0
    for part in message.walk():
        if part.is_multipart() and part.get_content_type() != "message/rfc822":
            continue

        filename = part.get_filename()
        if filename or part.get_content_disposition() == "attachment":
            attachments.append(
                ExtractedAttachment(
                    filename=filename or "attachment.bin", content=_attachment_bytes(part)
                )
            )
            continue

        content_type = part.get_content_type()
        if content_type == "text/plain":
            body_index += 1
            passages.append(
                ParsedPassage(
                    kind="email_part",
                    locator={"part": "body", "index": body_index, "content_type": content_type},
                    text=part.get_content(),
                )
            )
        elif content_type == "text/html":
            body_index += 1
            passages.append(
                ParsedPassage(
                    kind="email_part",
                    locator={"part": "body", "index": body_index, "content_type": content_type},
                    text=html_to_text(part.get_content()),
                )
            )
        # Other inline content types (images without filenames, etc.) carry
        # no extractable text and no filename to re-ingest under -- skipped.

    return ParseResult(passages=passages, attachments=attachments)
