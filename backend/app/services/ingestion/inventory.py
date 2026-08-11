"""Corpus inventory (docs/plan.md §3's INGEST step, first half): walk a
corpus directory deterministically, hash every file, and record both what
the file CLAIMS to be (extension -> declared_mime) and what its bytes SAY
it is (python-magic sniff -> sniffed_mime). The two are kept side by side
on `documents` deliberately -- a mismatch is a routing signal
(services/ingestion/classify.py), never silently reconciled.

sha256 is computed here once and reused everywhere downstream: the
content-addressed store key, the `documents.sha256` dedup key (plan §9
rule 2: identical content is parsed once), and the page-image cache key.
"""

from __future__ import annotations

import hashlib
import mimetypes
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import magic

# magic reads file headers; 16KB is far more than any format's magic bytes
# need while keeping the sniff O(1) in file size.
_SNIFF_BYTES = 16_384


@dataclass(frozen=True)
class InventoryItem:
    """One corpus file, hashed and sniffed -- everything classify.route()
    and the documents INSERT need, except the routing decision itself."""

    abs_path: Path
    rel_path: str
    filename: str
    byte_size: int
    sha256: str
    declared_mime: str | None  # from the extension (the file's claim)
    sniffed_mime: str  # from the bytes (the file's reality)


def sha256_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sniff_mime(content: bytes) -> str:
    """MIME type from magic bytes. Empty content is reported as
    'inode/x-empty' by libmagic -- normalized here so callers can treat
    the zero-byte case purely via byte_size."""
    if not content:
        return "inode/x-empty"
    return magic.from_buffer(content[:_SNIFF_BYTES], mime=True)


def declared_mime_for(filename: str) -> str | None:
    """The extension's claim, via the stdlib mimetypes table (returns None
    for extensions the table doesn't know -- e.g. `.msg`, `.bin`)."""
    declared, _ = mimetypes.guess_type(filename)
    return declared


def build_item(*, abs_path: Path, rel_path: str, content: bytes) -> InventoryItem:
    return InventoryItem(
        abs_path=abs_path,
        rel_path=rel_path,
        filename=abs_path.name,
        byte_size=len(content),
        sha256=sha256_of(content),
        declared_mime=declared_mime_for(abs_path.name),
        sniffed_mime=sniff_mime(content),
    )


def iter_corpus_files(corpus_dir: Path) -> Iterator[tuple[InventoryItem, bytes]]:
    """Yield (item, content) for every regular file under `corpus_dir`,
    in sorted-path order so a run over the same corpus ingests in the same
    order every time (reproducibility -- the same reason corpusgen is
    seeded). Hidden files (.DS_Store and friends) are skipped: they are
    filesystem noise, not corpus documents."""
    for path in sorted(corpus_dir.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        content = path.read_bytes()
        rel_path = str(path.relative_to(corpus_dir))
        yield build_item(abs_path=path, rel_path=rel_path, content=content), content
