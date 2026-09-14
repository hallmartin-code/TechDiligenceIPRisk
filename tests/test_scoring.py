"""Score calculation, weighting, evidence caps, confidence and uncertainty."""

from __future__ import annotations

import pytest

from app.analysis.risk_scoring import (
    DECK_ONLY_CEILING,
    DEFAULT_WEIGHTS,
    apply_caps,
    compute_scores,
    overall_from_average,
    rule_score,
    uncertainty_band,
    weighted_average,
)
from app.models.schemas import (
    CATEGORY_LABELS,
    CategoryScore,
    Classification,
    EvidenceStatus,
    Finding,
    Polarity,
    RedFlag,
    ScoreCategory as C,
    Severity,
    Topic,
)


def _finding(category=C.CYBERSECURITY_COMPLIANCE, polarity=Polarity.POSITIVE, weight=1.5,
             classification=Classification.FACT, planned=False, fid="F001") -> Finding:
    return Finding(id=fid, category=category, topic=Topic.SECURITY, statement="x", classification=classification,
                   polarity=polarity, weight=weight, slides=[2], excerpt="x", planned=planned)


def _flag(category, severity) -> RedFlag:
    return RedFlag(code="TEST", category=category, issue="Issue", severity=severity,
                   classification=Classification.FACT, evidence="S2", slides=[2], business_consequence="c",
                   diligence_question="q", mitigation="m")


def _scores(values: dict[C, int]) -> list[CategoryScore]:
    return [CategoryScore(category=c, label=CATEGORY_LABELS[c], score=values[c], weight=DEFAULT_WEIGHTS[c],
                          rationale="r", evidence_status=EvidenceStatus.SUPPORTED) for c in C]


def test_weights_match_specification():
    assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)
    assert DEFAULT_WEIGHTS[C.TECHNICAL_ARCHITECTURE] == 0.15
    assert DEFAULT_WEIGHTS[C.IP_OWNERSHIP] == 0.15
    assert DEFAULT_WEIGHTS[C.TECHNICAL_DOCUMENTATION] == 0.05
    assert DEFAULT_WEIGHTS[C.THIRD_PARTY_DEPENDENCY] == 0.05


def test_overall_score_bounds_and_midpoint():
    assert overall_from_average(weighted_average(_scores({c: 5 for c in C}))) == 100
    assert overall_from_average(weighted_average(_scores({c: 1 for c in C}))) == 0
    assert overall_from_average(3.0) == 50


def test_weighted_average_uses_category_weights():
    values = {c: 3 for c in C}
    values[C.IP_OWNERSHIP] = 5      # weight 0.15
    values[C.TECHNICAL_DOCUMENTATION] = 1  # weight 0.05
    expected = 3 + 0.15 * 2 - 0.05 * 2
    assert weighted_average(_scores(values)) == pytest.approx(expected)
    assert overall_from_average(expected) == round((expected - 1) / 4 * 100)


def test_no_evidence_scores_as_unverified():
    score, rationale = rule_score([], C.IP_OWNERSHIP, unverified_score=2)
    assert score == 2 and "No deck evidence" in rationale


def test_single_fact_cannot_reach_five():
    findings = [_finding(weight=3.0)]
    raw, _ = rule_score(findings, C.CYBERSECURITY_COMPLIANCE, 2)
    assert raw == 5
    capped, caps = apply_caps(C.CYBERSECURITY_COMPLIANCE, raw, findings, [], 2)
    assert capped == 4 and any("two corroborating" in c for c in caps)


def test_planned_items_earn_no_credit():
    findings = [_finding(planned=True, weight=0.0, classification=Classification.INFERENCE,
                         polarity=Polarity.NEUTRAL)]
    score, _ = rule_score(findings, C.CYBERSECURITY_COMPLIANCE, 2)
    assert score == 2


def test_inference_counts_half():
    fact, _ = rule_score([_finding(weight=2.0)], C.CYBERSECURITY_COMPLIANCE, 2)
    inference, _ = rule_score([_finding(weight=2.0, classification=Classification.INFERENCE)],
                              C.CYBERSECURITY_COMPLIANCE, 2)
    assert fact == 5 and inference == 4


@pytest.mark.parametrize("severity,limit", [(Severity.CRITICAL, 2), (Severity.HIGH, 3)])
def test_red_flags_cap_category(severity, limit):
    findings = [_finding(fid="F1"), _finding(fid="F2")]
    capped, caps = apply_caps(C.CYBERSECURITY_COMPLIANCE, 5, findings, [_flag(C.CYBERSECURITY_COMPLIANCE, severity)], 2)
    assert capped == limit and caps


def test_two_criticals_cap_at_one():
    flags = [_flag(C.IP_OWNERSHIP, Severity.CRITICAL), _flag(C.IP_OWNERSHIP, Severity.CRITICAL)]
    capped, _ = apply_caps(C.IP_OWNERSHIP, 4, [], flags, 2)
    assert capped == 1


def test_proposed_scores_are_capped_by_evidence():
    proposed = {c: (5, "model says strong") for c in C}
    summary = compute_scores([], [], unverified_score=2, proposed=proposed)
    assert all(item.score <= 3 for item in summary.categories)
    assert summary.get(C.IP_OWNERSHIP).score == 2


def test_exit_readiness_capped_by_critical_elsewhere():
    findings = [_finding(category=c, fid=f"F{i}{j}") for i, c in enumerate(C) for j in range(2)]
    summary = compute_scores(findings, [_flag(C.IP_OWNERSHIP, Severity.CRITICAL)], unverified_score=2)
    assert summary.get(C.EXIT_READINESS).score <= 2


def test_uncertainty_shrinks_with_confidence():
    assert uncertainty_band(20) > uncertainty_band(60) >= uncertainty_band(85)
    assert uncertainty_band(100) >= 4


def test_confidence_respects_deck_only_ceiling(strong):
    _, analysis = strong
    assert analysis.evidence_confidence <= DECK_ONLY_CEILING
    assert analysis.scores.uncertainty >= 4


def test_analysis_overall_matches_categories(strong, risky):
    for _, analysis in (strong, risky):
        expected = overall_from_average(weighted_average(analysis.scores.categories))
        assert analysis.scores.overall_score == expected
