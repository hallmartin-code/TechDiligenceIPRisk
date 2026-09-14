"""Every FACT must be traceable to the slide it came from, in both input formats."""

from __future__ import annotations

import pytest

from app.models.schemas import Classification
from app.utils.text import normalize_for_match
from tests.conftest import analyze_path


@pytest.mark.parametrize("fixture", ["strong", "risky"])
def test_fact_findings_quote_their_slides(fixture, request):
    deck, analysis = request.getfixturevalue(fixture)
    facts = [f for f in analysis.source_evidence if f.classification == Classification.FACT]
    assert facts
    for finding in facts:
        assert finding.slides, finding
        assert all(1 <= s <= deck.slide_count for s in finding.slides)
        slide_text = normalize_for_match(" ".join(deck.slide(s).full_text for s in finding.slides))
        assert normalize_for_match(finding.excerpt) in slide_text, finding.id


@pytest.mark.parametrize("fixture", ["strong", "risky"])
def test_snapshot_facts_quote_their_slides(fixture, request):
    deck, analysis = request.getfixturevalue(fixture)
    for name, field in analysis.company:
        if field.classification == Classification.FACT:
            assert field.slides, name
            text = normalize_for_match(" ".join(deck.slide(s).full_text for s in field.slides))
            assert normalize_for_match(field.excerpt) in text, name


def test_red_flag_evidence_cites_slides(risky):
    deck, analysis = risky
    for flag in analysis.red_flags:
        if flag.classification == Classification.NOT_PROVIDED:
            assert flag.slides == []
            continue
        assert flag.slides and all(1 <= s <= deck.slide_count for s in flag.slides)
        assert flag.evidence.startswith(f"S{flag.slides[0]}")


def test_specific_references_survive(risky, strong):
    _, risky_analysis = risky
    _, strong_analysis = strong
    flags = {f.code: f for f in risky_analysis.red_flags}
    assert flags["COPYLEFT_OPEN_SOURCE"].slides == [4]
    assert flags["IP_OWNERSHIP_UNCLEAR"].slides == [6]
    assert flags["CONTRACTOR_DEVELOPED_CODE"].slides == [4, 5]
    assert strong_analysis.company.revenue.slides == [3]
    patents = next(f for f in strong_analysis.source_evidence if f.signal_key == "ipd_issued_patents")
    assert patents.slides == [7]


def test_slide_references_match_across_formats(risky_pptx, risky_pdf):
    _, from_pptx = analyze_path(risky_pptx)
    _, from_pdf = analyze_path(risky_pdf)
    pptx_refs = {f.code: f.slides for f in from_pptx.red_flags}
    pdf_refs = {f.code: f.slides for f in from_pdf.red_flags}
    assert pptx_refs == pdf_refs
