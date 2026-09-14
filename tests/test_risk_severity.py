"""Red-flag detection and severity escalation rules."""

from __future__ import annotations

from app.models.schemas import SEVERITY_RANK, Classification, ScoreCategory as C, Severity


def flag(analysis, code):
    return next((f for f in analysis.red_flags if f.code == code), None)


def test_contractor_code_without_assignments_is_critical(analyze):
    analysis = analyze(("Technology", ["The platform was built by an outsourced development agency."]))
    contractor = flag(analysis, "CONTRACTOR_DEVELOPED_CODE")
    assert contractor.severity == Severity.CRITICAL
    assert contractor.classification == Classification.INFERENCE  # the ownership gap is inferred


def test_contractor_code_with_assignments_is_medium(analyze):
    analysis = analyze(
        ("Technology", ["The platform was built by an outsourced development agency."]),
        ("Intellectual Property", ["All employees and contractors have signed invention assignment agreements."]))
    assert flag(analysis, "CONTRACTOR_DEVELOPED_CODE").severity == Severity.MEDIUM
    assert flag(analysis, "IP_ASSIGNMENT_NOT_EVIDENCED") is None


def test_negated_copyleft_is_not_flagged(analyze):
    analysis = analyze(("Open Source", ["No GPL or AGPL components are used in the product."]))
    assert flag(analysis, "COPYLEFT_OPEN_SOURCE") is None


def test_copyleft_with_license_scanning_is_downgraded(analyze):
    analysis = analyze(("Open Source", ["Some GPL-licensed tools are used internally.",
                                        "Open-source license scanning with Snyk runs in CI."]))
    assert flag(analysis, "COPYLEFT_OPEN_SOURCE").severity == Severity.MEDIUM


def test_key_person_with_little_documentation_is_critical(analyze):
    analysis = analyze(("Team", ["Our CTO wrote the entire codebase and is the only engineer.",
                                 "There is little formal documentation."]))
    assert flag(analysis, "KEY_PERSON_DEPENDENCY").severity == Severity.CRITICAL
    assert analysis.engineering_organization.key_person_risk is True


def test_university_ip_with_unresolved_license_is_critical(analyze):
    analysis = analyze(("Intellectual Property", ["Our core technology is licensed from Stanford University.",
                                                  "The license terms are still being negotiated."]))
    assert flag(analysis, "UNIVERSITY_IP").severity == Severity.CRITICAL
    assert flag(analysis, "IP_OWNERSHIP_UNCLEAR").severity == Severity.CRITICAL
    assert flag(analysis, "LICENSED_CORE_TECHNOLOGY") is not None


def test_roadmap_security_is_not_credited(analyze):
    analysis = analyze(("Security", ["We will complete SOC 2 Type II certification by Q4 2027."]))
    soc2 = [f for f in analysis.source_evidence if f.signal_key == "cy_soc2"]
    assert soc2 and all(f.planned and f.weight == 0 and f.classification == Classification.INFERENCE for f in soc2)
    assert analysis.scores.get(C.CYBERSECURITY_COMPLIANCE).score <= 2
    gap = flag(analysis, "SECURITY_NOT_EVIDENCED")
    assert gap.classification == Classification.NOT_PROVIDED and "planned" in gap.evidence.lower()


def test_prototype_roadmap_is_not_production(analyze):
    analysis = analyze(("Product", ["Our prototype is working in the lab.",
                                    "We plan to launch in production with customers next year."]))
    assert analysis.development_maturity.level.value in ("Prototype", "Experimental")


def test_patents_alone_do_not_prove_defensibility(analyze):
    analysis = analyze(("Intellectual Property", ["We hold 3 issued U.S. patents on our sensor design."]))
    assert analysis.scores.get(C.IP_DEFENSIBILITY).score <= 4
    assert "not treated as proof" in analysis.intellectual_property.defensibility
    assert "freedom-to-operate" in analysis.intellectual_property.defensibility.lower()


def test_flags_sorted_most_severe_first(risky):
    _, analysis = risky
    ranks = [SEVERITY_RANK[f.severity] for f in analysis.red_flags]
    assert ranks == sorted(ranks)


def test_every_flag_carries_full_diligence_framing(risky):
    _, analysis = risky
    for item in analysis.red_flags:
        assert item.issue and item.evidence and item.business_consequence
        assert item.diligence_question.endswith("?") or item.diligence_question.startswith("Provide")
        assert item.mitigation and item.exposures
