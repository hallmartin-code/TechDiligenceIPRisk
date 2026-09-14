"""Malformed, unsupported and oversized inputs fail with user-readable errors and are logged."""

from __future__ import annotations

import io
import logging
import zipfile

import pytest

from app.config import Settings
from app.ingestion import ppt_parser
from app.ingestion.errors import (
    DeckIngestionError,
    DeckTooLargeError,
    MalformedDeckError,
    UnsupportedDeckError,
)
from app.ingestion.extractor import load_deck
from app.main import EXIT_INGESTION, main


def test_missing_file(tmp_path, rules_settings):
    with pytest.raises(DeckIngestionError, match="not found"):
        load_deck(tmp_path / "nope.pdf", rules_settings)


def test_empty_file(tmp_path, rules_settings):
    path = tmp_path / "empty.pdf"
    path.write_bytes(b"")
    with pytest.raises(MalformedDeckError, match="empty"):
        load_deck(path, rules_settings)


def test_garbage_named_pdf(tmp_path, rules_settings, caplog):
    path = tmp_path / "garbage.pdf"
    path.write_bytes(b"this is not a pdf at all" * 20)
    with pytest.raises(MalformedDeckError, match="PDF"):
        load_deck(path, rules_settings)


def test_corrupt_pdf_body_is_handled(tmp_path, rules_settings, caplog):
    path = tmp_path / "corrupt.pdf"
    path.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj << /Type /Catalog /Pages 2 0 R >>\nendobj\n" + b"\x00garbage" * 50)
    caplog.set_level(logging.WARNING, logger="tdip")
    with pytest.raises(DeckIngestionError):
        load_deck(path, rules_settings)
    assert any(record.levelno >= logging.WARNING for record in caplog.records), "parse failure must be logged"


def test_truncated_pdf_never_raises_unexpected_errors(tmp_path, risky_pdf, rules_settings):
    data = risky_pdf.read_bytes()
    path = tmp_path / "truncated.pdf"
    path.write_bytes(data[: len(data) // 2])
    try:
        deck = load_deck(path, rules_settings)
    except DeckIngestionError:
        return
    assert deck.slide_count >= 1


def test_zip_that_is_not_pptx(tmp_path, rules_settings):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "hello")
    path = tmp_path / "fake.pptx"
    path.write_bytes(buffer.getvalue())
    with pytest.raises(MalformedDeckError, match="not a PowerPoint"):
        load_deck(path, rules_settings)


def test_pptx_with_corrupt_xml(tmp_path, strong_pptx, rules_settings, caplog):
    caplog.set_level(logging.ERROR, logger="tdip")
    source = zipfile.ZipFile(strong_pptx)
    path = tmp_path / "corrupt.pptx"
    with zipfile.ZipFile(path, "w") as target:
        for item in source.infolist():
            content = b"<not-xml" if item.filename == "ppt/presentation.xml" else source.read(item.filename)
            target.writestr(item, content)
    with pytest.raises(MalformedDeckError):
        load_deck(path, rules_settings)
    assert any("could not be opened" in r.getMessage() for r in caplog.records)


def test_unsupported_extension(tmp_path, rules_settings):
    path = tmp_path / "memo.docx"
    path.write_bytes(b"PK\x03\x04")
    with pytest.raises(UnsupportedDeckError):
        load_deck(path, rules_settings)


def test_legacy_ppt_without_libreoffice(tmp_path, monkeypatch):
    monkeypatch.setattr(ppt_parser.shutil, "which", lambda _name: None)
    path = tmp_path / "old.ppt"
    path.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 512)
    with pytest.raises(UnsupportedDeckError, match="LibreOffice"):
        load_deck(path, Settings(engine="rules", soffice_path=None))


def test_oversized_file(risky_pdf):
    with pytest.raises(DeckTooLargeError):
        load_deck(risky_pdf, Settings(engine="rules", max_upload_mb=0.001))


def test_too_many_slides(risky_pdf):
    with pytest.raises(DeckTooLargeError):
        load_deck(risky_pdf, Settings(engine="rules", max_slides=3))


def test_cli_exit_code_for_malformed_deck(tmp_path):
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"junk")
    assert main(["analyze", str(path), "--engine", "rules", "--output-dir", str(tmp_path), "--quiet"]) == EXIT_INGESTION
