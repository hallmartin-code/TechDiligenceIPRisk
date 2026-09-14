"""Prioritized diligence questions.

Order of priority: questions attached to CRITICAL flags, then HIGH flags, then
questions the LLM engine raised, then MEDIUM flags, then baseline questions for
the weakest categories, then LOW flags. Near-duplicates are removed so the five
printed questions are five different lines of inquiry.
"""

from __future__ import annotations

import re

from app.analysis.red_flags import audience_for
from app.analysis.risk_scoring import DEFAULT_WEIGHTS
from app.models.schemas import (
    DiligenceQuestion,
    RedFlag,
    ScoreCategory as C,
    ScoreSummary,
    Severity,
)

BASELINE: tuple[tuple[C, str, str, str], ...] = (
    (C.IP_OWNERSHIP, "Who owns every component of the source code, including founder- and contractor-written code?",
     "IP counsel", "Software chain of title is the most common acquisition closing issue."),
    (C.IP_OWNERSHIP, "Are there university, government, customer or prior-employer claims on any core IP?",
     "IP counsel", "Third-party rights can require consents or reduce exit value."),
    (C.TECHNICAL_ARCHITECTURE, "Can you share an architecture diagram showing services, data stores, hosting and single points of failure?",
     "Technical advisor", "Architecture evidence is needed to assess resilience and scalability."),
    (C.CYBERSECURITY_COMPLIANCE, "Which security attestations (e.g., SOC 2 Type II) and penetration tests are current?",
     "CISO", "Enterprise buyers and acquirers expect documented controls."),
    (C.SCALABILITY, "What technical bottleneck appears first at 10x current volume?",
     "Technical advisor", "Identifies re-platforming spend hidden in the growth plan."),
    (C.ENGINEERING_ORGANIZATION, "Which systems depend on a single engineer?",
     "CTO", "Key-person dependency drives retention terms and integration risk."),
    (C.IP_DEFENSIBILITY, "Which patents or trade secrets directly protect the revenue-generating product?",
     "IP counsel", "Registered IP only matters if it covers what customers buy."),
    (C.THIRD_PARTY_DEPENDENCY, "Which third-party platform would be hardest to replace, and what are its pricing and change-of-control terms?",
     "CTO / Acquirer", "Dependencies affect margin, continuity and consent to close."),
    (C.TECHNICAL_DOCUMENTATION, "What technical debt would an acquirer inherit, and what would remediation cost?",
     "Acquirer", "Inherited debt is commonly handled through price adjustments."),
    (C.IP_OWNERSHIP, "What percentage of the codebase is open source, and which licenses impose copyleft or redistribution obligations?",
     "CTO", "Open-source obligations are a standard representation and warranty."),
    (C.DEVELOPMENT_MATURITY, "Which capabilities run in production with paying customers today versus on the roadmap?",
     "Technical advisor", "Separates completed development from planned development."),
    (C.EXIT_READINESS, "Has an independent code, security or IP audit been performed, and can results be shared?",
     "Acquirer", "Prior audits shorten diligence and reduce escrow asks."),
)


def _tokens(text: str) -> set[str]:
    stop = {"the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "is", "are", "what", "which", "how",
            "do", "does", "any", "every", "each", "you", "your", "it", "its", "with", "today", "has", "have", "be"}
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in stop and len(t) > 2}


def _similar(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.45


def build_questions(flags: list[RedFlag], scores: ScoreSummary,
                    extra: list[DiligenceQuestion] | None = None, limit: int = 12) -> list[DiligenceQuestion]:
    candidates: list[tuple[int, float, DiligenceQuestion]] = []

    def add(tier: int, weight: float, question: str, audience: str, rationale: str, category: C,
            code: str | None = None) -> None:
        candidates.append((tier, -weight, DiligenceQuestion(
            priority=1, question=question, audience=audience, rationale=rationale, category=category, flag_code=code)))

    tier_for = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 3, Severity.LOW: 5}
    for flag in flags:
        add(tier_for[flag.severity], DEFAULT_WEIGHTS.get(flag.category, 0), flag.diligence_question,
            audience_for(flag.code), f"{flag.severity.value}: {flag.issue}", flag.category, flag.code)
    for item in extra or []:
        add(2, DEFAULT_WEIGHTS.get(item.category, 0), item.question, item.audience, item.rationale, item.category,
            item.flag_code)
    for category, question, audience, rationale in BASELINE:
        score = scores.get(category).score
        # Weak categories come first; strong ones still get verification questions.
        add(4 if score <= 3 else 6, DEFAULT_WEIGHTS[category] * (6 - score), question, audience, rationale, category)

    candidates.sort(key=lambda c: (c[0], c[1]))
    chosen: list[DiligenceQuestion] = []
    for _, _, question in candidates:
        if any(_similar(question.question, existing.question) for existing in chosen):
            continue
        chosen.append(question)
        if len(chosen) >= limit:
            break
    return [q.model_copy(update={"priority": i}) for i, q in enumerate(chosen, 1)]
