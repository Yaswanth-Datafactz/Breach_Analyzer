"""Unit tests for the corpus walk (services/ingestion/inventory.py):
deterministic order, sha256 equality for identical content (the dedup
key), declared-vs-sniffed mime capture, hidden-file skipping. No DB."""

from __future__ import annotations

from pathlib import Path

from app.services.ingestion.inventory import iter_corpus_files, sha256_of


def test_identical_content_hashes_identically(tmp_path: Path):
    content = b"Name,SSN\nCasey Vance,531-24-8817\n"
    (tmp_path / "a_export.csv").write_bytes(content)
    (tmp_path / "b_copy.csv").write_bytes(content)
    (tmp_path / "c_other.txt").write_bytes(b"different content entirely")
    (tmp_path / ".DS_Store").write_bytes(b"filesystem noise")

    walked = list(iter_corpus_files(tmp_path))

    # Sorted-path order, hidden file skipped.
    assert [item.rel_path for item, _ in walked] == ["a_export.csv", "b_copy.csv", "c_other.txt"]

    by_name = {item.filename: item for item, _ in walked}
    assert by_name["a_export.csv"].sha256 == by_name["b_copy.csv"].sha256 == sha256_of(content)
    assert by_name["c_other.txt"].sha256 != by_name["a_export.csv"].sha256


def test_declared_and_sniffed_mime_are_both_recorded(tmp_path: Path):
    (tmp_path / "export.csv").write_bytes(b"Name,SSN\nCasey Vance,531-24-8817\n")
    ((item, content),) = list(iter_corpus_files(tmp_path))
    assert item.declared_mime == "text/csv"
    assert item.sniffed_mime in ("text/csv", "text/plain")
    assert item.byte_size == len(content)


def test_zero_byte_file_is_inventoried_not_dropped(tmp_path: Path):
    (tmp_path / "empty.txt").write_bytes(b"")
    ((item, _),) = list(iter_corpus_files(tmp_path))
    assert item.byte_size == 0
    assert item.sniffed_mime == "inode/x-empty"


def test_nested_directories_produce_relative_paths(tmp_path: Path):
    nested = tmp_path / "batch1" / "hr"
    nested.mkdir(parents=True)
    (nested / "memo.txt").write_bytes(b"nested memo")
    ((item, _),) = list(iter_corpus_files(tmp_path))
    assert item.rel_path == str(Path("batch1") / "hr" / "memo.txt")
