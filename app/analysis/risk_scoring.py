"""Category scores, evidence caps, overall score, evidence confidence and ratings.

Scoring is always done here, in Python, whichever engine produced the
findings. The LLM engine may *propose* category scores; they pass through the
same evidence caps as rule-based scores:

* a 5 needs at least two corroborating positive deck facts;
* a 4 or better needs at least one positive deck fact;
* a category with no evidence at all receives the configured unverified score;
* a CRITICAL red flag caps its category at 2 (1 if there are two), HIGH at 3.

The overall score is reported with an uncertainty band derived from evidence
confidence, so thin decks do not produce falsely precise numbers.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from app.models.schemas import (
    CATEGORY_LABELS,
    IP_CATEGORIES,
    TECHNICAL_CATEGORIES,
    CategoryScore,
    Classification,
    CompanySnapshot,
    EvidenceStatus,
    Finding,
    Polarity,
    PositionRating,
    RedFlag,
    ScoreCategory as C,
    ScoreSummary,
    Severity,
    SourceDocument,
    TransactionReadiness,
)

DEFAULT_WEIGHTS: dict[C, float] = {
    C.TECHNICAL_ARCHITECTURE: 0.15,
    C.DEVELOPMENT_MATURITY: 0.10,
    C.SCALABILITY: 0.10,
    C.ENGINEERING_ORGANIZATION: 0.10,
    C.TECHNICAL_DOCUMENTATION: 0.05,
    C.CYBERSECURITY_COMPLIANCE: 0.10,
    C.IP_OWNERSHIP: 0.15,
    C.IP_DEFENSIBILITY: 0.10,
    C.THIRD_PARTY_DEPENDENCY: 0.05,
    C.EXIT_READINESS: 0.10,
}

_CLASS_FACTOR = {Classification.FACT: 1.0, Classification.INFERENCE: 0.5}
POSITIVE_CAP = 2.25
NEGATIVE_CAP = 2.5

# A pitch deck is self-reported: even a complete one is claims, not verified
# evidence, so confidence never reaches the level a data-room review would.
DECK_ONLY_CEILING = 85

# Exit readiness is largely a function of the other categories.
_EXIT_BASE_WEIGHTS = {
    C.IP_OWNERSHIP: 0.30, C.CYBERSECURITY_COMPLIANCE: 0.20, C.TECHNICAL_DOCUMENTATION: 0.15,
    C.ENGINEERING_ORGANIZATION: 0.15, C.TECHNICAL_ARCHITECTURE: 0.10, C.THIRD_PARTY_DEPENDENCY: 0.10,
}


def _relevance(finding: Finding, category: C) -> float:
    if finding.category == category:
        return 1.0
    if category in finding.secondary_categories:
        return 0.5
    return 0.0


def _relevant(findings: Iterable[Finding], category: C) -> list[tuple[Finding, float]]:
    return [(f, r) for f in findings if (r := _relevance(f, category)) > 0]


def evidence_status(findings: list[Finding], category: C) -> EvidenceStatus:
    facts = [f for f, _ in _relevant(findings, category)
             if f.classification == Classification.FACT and not f.planned and f.weight > 0]
    if len(facts) >= 2:
        return EvidenceStatus.SUPPORTED
    if facts or any(f.classification == Classification.INFERENCE and f.weight > 0 and not f.planned
                    for f, _ in _relevant(findings, category)):
        return EvidenceStatus.LIMITED
    return EvidenceStatus.NOT_PROVIDED


def rule_score(findings: list[Finding], category: C, unverified_score: int) -> tuple[int, str]:
    """Score a category from weighted evidence around a neutral 3."""
    relevant = [(f, r) for f, r in _relevant(findings, category) if not f.planned and f.weight > 0]
    counted = [(f, r) for f, r in relevant if f.classification in _CLASS_FACTOR]
    if not counted:
        return unverified_score, "No deck evidence; scored as unverified for later-stage diligence."
    positive = sum(f.weight * r * _CLASS_FACTOR[f.classification] for f, r in counted if f.polarity == Polarity.POSITIVE)
    negative = sum(f.weight * r * _CLASS_FACTOR[f.classification] for f, r in counted if f.polarity == Polarity.NEGATIVE)
    raw = 3.0 + min(positive, POSITIVE_CAP) - min(negative, NEGATIVE_CAP)
    score = max(1, min(5, math.floor(raw + 0.5)))
    pos_labels = [f.statement for f, _ in counted if f.polarity == Polarity.POSITIVE][:2]
    neg_labels = [f.statement for f, _ in counted if f.polarity == Polarity.NEGATIVE][:2]
    parts = []
    if pos_labels:
        parts.append("Supported by: " + "; ".join(pos_labels))
    if neg_labels:
        parts.append("Offset by: " + "; ".join(neg_labels))
    return score, ". ".join(parts) + "."


def apply_caps(category: C, score: int, findings: list[Finding], flags: list[RedFlag],
               unverified_score: int) -> tuple[int, list[str]]:
    caps: list[str] = []
    relevant = _relevant(findings, category)
    positive_facts = sum(1 for f, _ in relevant if f.classification == Classification.FACT and not f.planned
                         and f.polarity == Polarity.POSITIVE and f.weight > 0)
    any_evidence = any(not f.planned and f.weight > 0 and f.classification in _CLASS_FACTOR for f, _ in relevant)

    def cap(limit: int, reason: str) -> None:
        nonlocal score
        if score > limit:
            score = limit
            caps.append(reason)

    if not any_evidence and category != C.EXIT_READINESS:
        cap(unverified_score, "No deck evidence for this category")
    if positive_facts < 2:
        cap(4, "A 5 requires at least two corroborating deck facts")
    if positive_facts < 1:
        cap(3, "No positive deck facts support a score above 3")
    criticals = [f for f in flags if f.category == category and f.severity == Severity.CRITICAL]
    highs = [f for f in flags if f.category == category and f.severity == Severity.HIGH]
    if len(criticals) >= 2:
        cap(1, "Multiple critical red flags")
    elif criticals:
        cap(2, f"Critical red flag: {criticals[0].issue}")
    if highs:
        cap(3, f"High-severity red flag: {highs[0].issue}")
    if category == C.EXIT_READINESS:
        all_critical = [f for f in flags if f.severity == Severity.CRITICAL]
        all_high = [f for f in flags if f.severity == Severity.HIGH]
        if all_critical:
            cap(2, "Critical issue elsewhere would surface in transaction diligence")
        elif len(all_high) >= 3:
            cap(3, "Three or more high-severity diligence issues")
    return max(1, score), caps


def compute_scores(findings: list[Finding], flags: list[RedFlag], unverified_score: int,
                   proposed: dict[C, tuple[int, str]] | None = None,
                   weights: dict[C, float] | None = None) -> ScoreSummary:
    weights = weights or DEFAULT_WEIGHTS
    results: dict[C, CategoryScore] = {}
    for category in C:
        if category == C.EXIT_READINESS:
            continue
        if proposed and category in proposed:
            base, rationale = proposed[category]
            base = max(1, min(5, int(base)))
        else:
            base, rationale = rule_score(findings, category, unverified_score)
        score, caps = apply_caps(category, base, findings, flags, unverified_score)
        results[category] = CategoryScore(
            category=category, label=CATEGORY_LABELS[category], score=score, weight=weights[category],
            rationale=rationale, evidence_status=evidence_status(findings, category), caps_applied=caps)

    exit_cat = C.EXIT_READINESS
    if proposed and exit_cat in proposed:
        exit_base, exit_rationale = proposed[exit_cat]
    else:
        base = sum(results[c].score * w for c, w in _EXIT_BASE_WEIGHTS.items())
        relevant = [(f, r) for f, r in _relevant(findings, exit_cat) if not f.planned and f.weight > 0]
        delta = sum((1 if f.polarity == Polarity.POSITIVE else -1) * f.weight * r * _CLASS_FACTOR.get(f.classification, 0)
                    for f, r in relevant if f.polarity != Polarity.NEUTRAL)
        exit_base = math.floor(base + max(-1.0, min(1.0, delta)) + 0.5)
        exit_rationale = ("Derived from IP ownership, security, documentation, engineering and dependency scores"
                          + (", adjusted for exit-specific deck evidence." if relevant else "."))
    exit_score, exit_caps = apply_caps(exit_cat, max(1, min(5, int(exit_base))), findings, flags, unverified_score)
    results[exit_cat] = CategoryScore(
        category=exit_cat, label=CATEGORY_LABELS[exit_cat], score=exit_score, weight=weights[exit_cat],
        rationale=exit_rationale, evidence_status=evidence_status(findings, exit_cat), caps_applied=exit_caps)

    categories = [results[c] for c in C]
    weighted = weighted_average(categories)
    return ScoreSummary(categories=categories, weighted_average=round(weighted, 3),
                        overall_score=overall_from_average(weighted), uncertainty=0)


def weighted_average(categories: list[CategoryScore]) -> float:
    total_weight = sum(c.weight for c in categories)
    return sum(c.score * c.weight for c in categories) / total_weight


def overall_from_average(weighted: float) -> int:
    """Map a 1-5 weighted average onto 0-100 (1 -> 0, 5 -> 100)."""
    return int(round((weighted - 1.0) / 4.0 * 100))


def uncertainty_band(confidence: int) -> int:
    return int(max(4, min(20, round((100 - confidence) * 0.2))))


def evidence_confidence(findings: list[Finding], snapshot: CompanySnapshot, source: SourceDocument,
                        grounding_rejections: int = 0) -> int:
    scored = [c for c in C if c != C.EXIT_READINESS]
    facts = [f for f in findings if f.classification == Classification.FACT and not f.planned]
    covered = sum(1 for c in scored if any(_relevance(f, c) > 0 for f in facts))
    coverage = 45.0 * covered / len(scored)
    depth = 20.0 * min(len(facts), 30) / 30

    fields = [snapshot.sector, snapshot.stage, snapshot.revenue, snapshot.growth, snapshot.gross_margin,
              snapshot.customers, snapshot.raise_amount, snapshot.product]
    provided = sum(1.0 if f.classification == Classification.FACT else 0.5 for f in fields if f.provided)
    snapshot_points = 15.0 * provided / len(fields)

    slides = max(source.slide_count, 1)
    quality = 20.0 * (slides - len(source.image_only_slides)) / slides
    if source.slide_count < 4:
        quality *= 0.5
    other_warnings = [w for w in source.parse_warnings if "image-only" not in w]
    quality -= min(10.0, 2.0 * len(other_warnings))
    quality -= min(10.0, float(grounding_rejections))
    total = coverage + depth + snapshot_points + max(0.0, quality)
    return int(max(0, min(DECK_ONLY_CEILING, round(total))))


def _band(value: float) -> PositionRating:
    if value >= 4.25:
        return PositionRating.STRONG
    if value >= 3.5:
        return PositionRating.ADEQUATE
    if value >= 2.75:
        return PositionRating.MIXED
    if value >= 2.0:
        return PositionRating.WEAK
    return PositionRating.HIGH_RISK


_RATING_ORDER = [PositionRating.STRONG, PositionRating.ADEQUATE, PositionRating.MIXED, PositionRating.WEAK,
                 PositionRating.HIGH_RISK]


def _at_most(rating: PositionRating, ceiling: PositionRating) -> PositionRating:
    return _RATING_ORDER[max(_RATING_ORDER.index(rating), _RATING_ORDER.index(ceiling))]


def _group_average(scores: ScoreSummary, group: tuple[C, ...]) -> float:
    items = [scores.get(c) for c in group]
    return sum(i.score * i.weight for i in items) / sum(i.weight for i in items)


def technical_position(scores: ScoreSummary, flags: list[RedFlag]) -> PositionRating:
    rating = _band(_group_average(scores, TECHNICAL_CATEGORIES))
    critical = [f for f in flags if f.severity == Severity.CRITICAL and f.category in TECHNICAL_CATEGORIES]
    if len(critical) >= 2:
        return PositionRating.HIGH_RISK
    if critical:
        return _at_most(rating, PositionRating.WEAK)
    return rating


def ip_position(scores: ScoreSummary, flags: list[RedFlag]) -> PositionRating:
    rating = _band(_group_average(scores, IP_CATEGORIES))
    critical = [f for f in flags if f.severity == Severity.CRITICAL and f.category in IP_CATEGORIES]
    if any(f.category == C.IP_OWNERSHIP for f in critical) and len(critical) >= 2:
        return PositionRating.HIGH_RISK
    if critical:
        return _at_most(rating, PositionRating.WEAK)
    return rating


def transaction_readiness(scores: ScoreSummary, flags: list[RedFlag], confidence: int) -> TransactionReadiness:
    critical = sum(1 for f in flags if f.severity == Severity.CRITICAL)
    high = sum(1 for f in flags if f.severity == Severity.HIGH)
    exit_score = scores.get(C.EXIT_READINESS).score
    if critical or exit_score <= 1:
        result = TransactionReadiness.SIGNIFICANT_REMEDIATION
    elif exit_score <= 2 or high >= 3:
        result = TransactionReadiness.MATERIAL_DILIGENCE
    elif scores.overall_score >= 75 and high == 0 and confidence >= 60:
        result = TransactionReadiness.READY
    elif scores.overall_score >= 60 and high <= 1:
        result = TransactionReadiness.MOSTLY_READY
    else:
        result = TransactionReadiness.MATERIAL_DILIGENCE
    if confidence < 40 and result in (TransactionReadiness.READY, TransactionReadiness.MOSTLY_READY):
        result = TransactionReadiness.MATERIAL_DILIGENCE
    return result
