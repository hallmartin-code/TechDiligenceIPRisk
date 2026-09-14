"""The clean-IP company and the high-risk company must be clearly separated."""

from __future__ import annotations

import pytest

from app.models.schemas import PositionRating, ScoreCategory as C, Severity, TransactionReadiness
from app.utils.validation import validate_analysis
from tests.conftest import analyze_path


def test_high_risk_company_receives_materially_higher_diligence_risk(strong, risky):
    _, good = strong
    _, bad = risky
    assert bad.scores.overall_score <= good.scores.overall_score - 40
    assert len(bad.flags_at(Severity.CRITICAL)) >= 3
    assert len(bad.flags_at(Severity.CRITICAL, Severity.HIGH)) >= 8
    assert not good.flags_at(Severity.CRITICAL, Severity.HIGH)
    for category in (C.IP_OWNERSHIP, C.ENGINEERING_ORGANIZATION, C.TECHNICAL_DOCUMENTATION,
                     C.TECHNICAL_ARCHITECTURE, C.EXIT_READINESS):
        assert bad.scores.get(category).score < good.scores.get(category).score, category


def test_high_risk_company_issues_are_named(risky):
    _, analysis = risky
    codes = {f.code for f in analysis.red_flags}
    assert {"CONTRACTOR_DEVELOPED_CODE", "COPYLEFT_OPEN_SOURCE", "KEY_PERSON_DEPENDENCY", "UNDOCUMENTED_CODEBASE",
            "IP_OWNERSHIP_UNCLEAR", "UNIVERSITY_IP", "FOUNDATION_MODEL_DEPENDENCY"} <= codes
    assert analysis.investor_conclusion.transaction_readiness == TransactionReadiness.SIGNIFICANT_REMEDIATION
    assert analysis.investor_conclusion.ip_position in (PositionRating.WEAK, PositionRating.HIGH_RISK)
    assert analysis.investor_conclusion.technical_position in (PositionRating.WEAK, PositionRating.HIGH_RISK)
    assert analysis.ai_data.applicable
    assert analysis.ai_data.proprietary_assessment.startswith("INFERENCE")


def test_strong_company_conclusions(strong):
    _, analysis = strong
    conclusion = analysis.investor_conclusion
    assert conclusion.technical_position in (PositionRating.STRONG, PositionRating.ADEQUATE)
    assert conclusion.ip_position in (PositionRating.STRONG, PositionRating.ADEQUATE)
    assert conclusion.transaction_readiness in (TransactionReadiness.READY, TransactionReadiness.MOSTLY_READY)
    planned = [f for f in analysis.source_evidence if f.planned]
    assert any("FedRAMP" in f.statement for f in planned)  # roadmap item not credited
    assert len(analysis.diligence_questions) >= 5


@pytest.mark.parametrize("pptx_fixture,pdf_fixture", [("strong_pptx", "strong_pdf"), ("risky_pptx", "risky_pdf")])
def test_results_consistent_across_input_formats(pptx_fixture, pdf_fixture, request):
    _, from_pptx = analyze_path(request.getfixturevalue(pptx_fixture))
    _, from_pdf = analyze_path(request.getfixturevalue(pdf_fixture))
    assert abs(from_pptx.scores.overall_score - from_pdf.scores.overall_score) <= 5
    assert {f.code for f in from_pptx.flags_at(Severity.CRITICAL)} == {f.code for f in from_pdf.flags_at(Severity.CRITICAL)}


def test_analyses_pass_validation(strong, risky):
    for _, analysis in (strong, risky):
        validate_analysis(analysis)
