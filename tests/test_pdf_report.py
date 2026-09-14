"""One-page PDF constraint, content presence, long names and overflow compression."""

from __future__ import annotations

import logging

from pypdf import PdfReader

from app.models.schemas import SnapshotField, Classification
from app.reporting.pdf_generator import generate_pdf, page_count, render_pdf_bytes


def _text(data_or_path) -> str:
    reader = PdfReader(data_or_path) if not isinstance(data_or_path, bytes) else PdfReader(__import__("io").BytesIO(data_or_path))
    return " ".join(" ".join((page.extract_text() or "") for page in reader.pages).split())


def test_sample_companies_render_on_one_page(strong, risky, tmp_path, rules_settings):
    for name, (_, analysis) in (("strong", strong), ("risky", risky)):
        path = generate_pdf(analysis, tmp_path / f"{name}.pdf", rules_settings)
        assert page_count(path.read_bytes()) == 1
        text = _text(path)
        for heading in ("DILIGENCE REVIEW", "COMPANY SNAPSHOT", "TECHNICAL ASSESSMENT", "INTELLECTUAL PROPERTY",
                        "TOP RISKS", "EXIT / DEAL-CLOSING EXPOSURE", "PRIORITY DILIGENCE QUESTIONS",
                        "INVESTOR CONCLUSION", "Compiled on", "TEN Capital Network"):
            assert heading in text, (name, heading)
        assert analysis.company.display_name() in text


def test_top_risks_and_questions_are_printed(risky, rules_settings):
    _, analysis = risky
    text = _text(render_pdf_bytes(analysis, rules_settings))
    assert "CRITICAL" in text
    assert analysis.red_flags[0].issue[:25] in text
    assert analysis.diligence_questions[0].question[:25] in text
    assert analysis.investor_conclusion.transaction_readiness.value.split()[0] in text


def test_long_company_name_stays_on_one_page(risky, rules_settings):
    _, analysis = risky
    long_name = "Very Long Company Name Holdings International " * 7
    variant = analysis.model_copy(deep=True)
    variant.company.name = SnapshotField(value=long_name.strip(), classification=Classification.FACT, slides=[1],
                                         excerpt="x")
    data = render_pdf_bytes(variant, rules_settings)
    assert page_count(data) == 1
    assert "Very Long Company Name Holdings" in _text(data)


def test_unicode_company_name(risky, rules_settings):
    _, analysis = risky
    variant = analysis.model_copy(deep=True)
    variant.company.name = SnapshotField(value="Société Générale Robotique — 株式会社", slides=[1], excerpt="x",
                                         classification=Classification.FACT)
    assert page_count(render_pdf_bytes(variant, rules_settings)) == 1


def test_overflowing_content_is_compressed_not_paginated(risky, rules_settings, caplog):
    caplog.set_level(logging.INFO, logger="tdip")
    _, analysis = risky
    long = "Extensive diligence commentary with many qualifying clauses and supporting detail. " * 30
    variant = analysis.model_copy(deep=True)
    variant.red_flags = [f.model_copy(update={"issue": long, "evidence": long, "business_consequence": long,
                                              "mitigation": long}) for f in analysis.red_flags] * 3
    for section in (variant.technical_architecture, variant.scalability, variant.engineering_organization,
                    variant.cybersecurity):
        section.summary = long
    variant.development_maturity.rationale = long
    ip = variant.intellectual_property
    for attr in ("core_ip", "ownership", "defensibility", "patents", "trade_secrets", "software_data", "third_party"):
        setattr(ip, attr, long)
    variant.exit_readiness.summary = long
    variant.diligence_questions = [q.model_copy(update={"question": f"Question {q.priority}: {long}"})
                                   for q in analysis.diligence_questions]
    variant.investor_conclusion.summary = long

    data = render_pdf_bytes(variant, rules_settings)
    assert page_count(data) == 1
    text = _text(data)
    assert "Question 1:" in text and "Question 5:" in text  # questions preserved
    assert "INVESTOR CONCLUSION" in text and "TRANSACTION READINESS" in text  # conclusion preserved
    levels = [r.getMessage() for r in caplog.records if "density level" in r.getMessage() or "shrinking" in r.getMessage()]
    assert levels and "level 0" not in levels[-1]
