"""Missing fields, missing IP information and missing technical information are labeled NOT PROVIDED."""

from __future__ import annotations

from app.models.schemas import (
    NOT_PROVIDED,
    Classification,
    EvidenceStatus,
    MaturityLevel,
    ScoreCategory as C,
    TransactionReadiness,
)
from app.reporting.pdf_generator import page_count, render_pdf_bytes
from app.utils.validation import validate_analysis


def test_deck_with_no_technical_or_ip_content(analyze, rules_settings):
    analysis = analyze(("The Problem", ["Restaurants waste 30% of food inventory every week."]),
                       ("Market", ["The global food waste market is large and growing."]))

    for name in ("revenue", "growth", "gross_margin", "customers", "raise_amount", "stage"):
        field = getattr(analysis.company, name)
        assert field.classification == Classification.NOT_PROVIDED and field.value == NOT_PROVIDED, name

    for category in (C.IP_OWNERSHIP, C.IP_DEFENSIBILITY, C.TECHNICAL_ARCHITECTURE, C.CYBERSECURITY_COMPLIANCE):
        item = analysis.scores.get(category)
        assert item.score == rules_settings.unverified_score
        assert item.evidence_status == EvidenceStatus.NOT_PROVIDED

    flags = {f.code: f for f in analysis.red_flags}
    for code in ("PATENT_POSITION_NOT_PROVIDED", "ARCHITECTURE_NOT_DESCRIBED", "IP_ASSIGNMENT_NOT_EVIDENCED",
                 "SECURITY_NOT_EVIDENCED", "TECH_LEADERSHIP_NOT_IDENTIFIED"):
        assert flags[code].classification == Classification.NOT_PROVIDED
        assert flags[code].slides == []

    items = {m.item for m in analysis.missing_information}
    assert {"Patent position", "Invention-assignment agreements", "Architecture description"} <= items
    assert analysis.intellectual_property.patents.startswith(NOT_PROVIDED)
    assert analysis.development_maturity.level == MaturityLevel.NOT_DETERMINABLE
    assert analysis.evidence_confidence < 40
    assert analysis.investor_conclusion.transaction_readiness in (
        TransactionReadiness.MATERIAL_DILIGENCE, TransactionReadiness.SIGNIFICANT_REMEDIATION)
    validate_analysis(analysis)
    assert page_count(render_pdf_bytes(analysis, rules_settings)) == 1


def test_missing_ip_information_only(analyze):
    analysis = analyze(("Platform Architecture", [
        "Cloud-native microservices on Kubernetes deployed multi-region with automated failover.",
        "CI/CD with GitHub Actions and 90% automated test coverage.",
        "99.9% uptime SLA over the last year."]))
    assert analysis.scores.get(C.TECHNICAL_ARCHITECTURE).score >= 4
    assert analysis.scores.get(C.IP_OWNERSHIP).score == 2
    assert analysis.scores.get(C.IP_DEFENSIBILITY).score == 2
    assert analysis.intellectual_property.core_ip.startswith(NOT_PROVIDED)
    assert "NOT PROVIDED" in analysis.intellectual_property.ownership
    assert any(f.code == "PATENT_POSITION_NOT_PROVIDED" for f in analysis.red_flags)


def test_missing_technical_information_only(analyze):
    analysis = analyze(("Intellectual Property", [
        "5 issued U.S. patents assigned to the company.",
        "All employees have signed invention assignment agreements."]))
    assert analysis.scores.get(C.TECHNICAL_ARCHITECTURE).score == 2
    assert "Architecture description" in analysis.technical_architecture.gaps
    assert "Not provided" in analysis.technical_architecture.summary
    assert analysis.development_maturity.level == MaturityLevel.NOT_DETERMINABLE
    assert analysis.scores.get(C.IP_OWNERSHIP).score >= 4
