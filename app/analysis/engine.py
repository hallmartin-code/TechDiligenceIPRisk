"""Analysis orchestration: engine selection, the rules engine, and `finalize`.

`finalize` is shared by both engines. It owns everything numeric and every
rating (scores, caps, confidence, positions, readiness), so an LLM can inform
the analysis but can never set a score that the evidence does not support.
"""

from __future__ import annotations

from typing import Any

from app.analysis import ip_analysis, technical_analysis
from app.analysis.diligence_questions import build_questions
from app.analysis.draft import AnalysisDraft
from app.analysis.red_flags import derive_flags, gap_flags, sort_flags
from app.analysis.risk_scoring import (
    compute_scores,
    evidence_confidence,
    ip_position,
    technical_position,
    transaction_readiness,
    uncertainty_band,
)
from app.analysis.signals import detect_dependencies, detect_signals, is_regulated_or_enterprise, signals_to_findings
from app.config import Settings, get_settings
from app.ingestion.document import DeckDocument
from app.ingestion.extractor import extract_company_snapshot
from app.models.schemas import (
    Assumption,
    Classification,
    DiligenceAnalysis,
    EngineInfo,
    ExitReadinessAssessment,
    ExposureItem,
    InvestorConclusion,
    MissingInformation,
    RedFlag,
    ScoreCategory as C,
    ScoreSummary,
    Severity,
    SourceDocument,
    TransactionReadiness,
)
from app.utils.logging import get_logger
from app.utils.text import lower_first as _lower_first
from app.utils.text import shorten

log = get_logger("analysis")

_GAP_IMPORTANCE = {
    C.IP_OWNERSHIP: Severity.HIGH, C.CYBERSECURITY_COMPLIANCE: Severity.HIGH, C.TECHNICAL_ARCHITECTURE: Severity.MEDIUM,
    C.DEVELOPMENT_MATURITY: Severity.MEDIUM, C.ENGINEERING_ORGANIZATION: Severity.MEDIUM,
    C.IP_DEFENSIBILITY: Severity.MEDIUM, C.SCALABILITY: Severity.MEDIUM, C.TECHNICAL_DOCUMENTATION: Severity.LOW,
    C.THIRD_PARTY_DEPENDENCY: Severity.LOW, C.EXIT_READINESS: Severity.MEDIUM,
}

STANDARD_ASSUMPTIONS = (
    Assumption(statement="Deck statements are taken as accurate at the deck date; no data room, source code, "
                         "contracts or security reports were reviewed.",
               classification=Classification.ASSUMPTION, basis="Scope limited to the uploaded pitch deck."),
    Assumption(statement="Categories with no deck evidence are scored as unverified rather than neutral.",
               classification=Classification.ASSUMPTION, basis="Later-stage diligence requires documentation."),
)


class AnalysisError(RuntimeError):
    """Analysis could not be completed. The message is safe to show to a user."""


def analyze_deck(deck: DeckDocument, settings: Settings | None = None, client: Any | None = None,
                 engine: str | None = None, source_bytes: bytes | None = None) -> DiligenceAnalysis:
    """Analyze an extracted deck. `source_bytes` (the original PDF) lets Claude see diagrams."""
    settings = settings or get_settings()
    requested = engine or settings.engine
    mode = requested
    if mode == "auto":
        mode = "llm" if (client is not None or settings.llm_credentials_available()) else "rules"

    if mode == "llm":
        from app.analysis.llm_engine import LLMEngineError, build_llm_draft

        try:
            draft = build_llm_draft(deck, settings, client, pdf_bytes=source_bytes)
        except LLMEngineError as exc:
            if requested == "llm":
                raise AnalysisError(str(exc)) from exc
            log.warning("LLM engine unavailable (%s); falling back to rules engine", exc)
            draft = build_rules_draft(deck, settings)
            draft.engine = EngineInfo(name="rules", fallback_reason=f"LLM engine failed: {exc}")
            draft.warnings.append(f"LLM analysis failed ({exc}); deterministic rules engine used instead.")
    else:
        draft = build_rules_draft(deck, settings)
        if requested == "auto":
            draft.engine = EngineInfo(name="rules", fallback_reason="No Anthropic credentials configured.")
    return finalize(draft, deck, settings)


def build_rules_draft(deck: DeckDocument, settings: Settings | None = None) -> AnalysisDraft:
    snapshot = extract_company_snapshot(deck)
    signals = detect_signals(deck, snapshot)
    findings = signals_to_findings(signals)
    dependencies = detect_dependencies(deck, signals)
    flags = derive_flags(signals, is_regulated_or_enterprise(deck))

    draft = AnalysisDraft(
        engine=EngineInfo(name="rules"),
        snapshot=snapshot,
        findings=findings,
        technical_architecture=technical_analysis.assess_architecture(findings, deck),
        development_maturity=technical_analysis.assess_maturity(findings, snapshot),
        engineering_organization=technical_analysis.assess_engineering(findings),
        technical_documentation=technical_analysis.assess_documentation(findings),
        cybersecurity=technical_analysis.assess_cybersecurity(findings, deck),
        intellectual_property=ip_analysis.assess_ip(findings, dependencies),
        ai_data=ip_analysis.assess_ai(findings, dependencies, deck),
        scalability=technical_analysis.assess_scalability(findings),
        dependencies=dependencies,
        flags=flags,
    )
    draft.missing = missing_from_sections(draft)
    draft.assumptions = list(STANDARD_ASSUMPTIONS) + _rules_assumptions(draft, deck)
    return draft


def missing_from_sections(draft: AnalysisDraft) -> list[MissingInformation]:
    sections = [
        (C.TECHNICAL_ARCHITECTURE, draft.technical_architecture.gaps),
        (C.DEVELOPMENT_MATURITY, draft.development_maturity.gaps),
        (C.ENGINEERING_ORGANIZATION, draft.engineering_organization.gaps),
        (C.TECHNICAL_DOCUMENTATION, draft.technical_documentation.gaps),
        (C.CYBERSECURITY_COMPLIANCE, draft.cybersecurity.gaps),
        (C.SCALABILITY, draft.scalability.gaps),
        (C.IP_OWNERSHIP, [g for g in draft.intellectual_property.gaps if g in _labels(C.IP_OWNERSHIP)]),
        (C.IP_DEFENSIBILITY, [g for g in draft.intellectual_property.gaps if g in _labels(C.IP_DEFENSIBILITY)]),
        (C.THIRD_PARTY_DEPENDENCY, [g for g in draft.intellectual_property.gaps if g in _labels(C.THIRD_PARTY_DEPENDENCY)]),
    ]
    if draft.ai_data.applicable:
        sections.append((C.IP_OWNERSHIP, draft.ai_data.gaps))
    seen: set[str] = set()
    missing = []
    for category, gaps in sections:
        for gap in gaps:
            if gap.lower() in seen:
                continue
            seen.add(gap.lower())
            missing.append(MissingInformation(item=gap, category=category, importance=_GAP_IMPORTANCE[category]))
    for field_name, label in (("revenue", "Revenue"), ("gross_margin", "Gross margin"), ("customers", "Customer base"),
                              ("raise_amount", "Raise amount")):
        if not getattr(draft.snapshot, field_name).provided:
            missing.append(MissingInformation(item=label, category=C.EXIT_READINESS, importance=Severity.LOW))
    return sorted(missing, key=lambda m: ["CRITICAL", "HIGH", "MEDIUM", "LOW"].index(m.importance.value))


def _labels(category: C) -> set[str]:
    labels = {label for label, _ in technical_analysis.EXPECTATIONS.get(category, [])}
    return labels | {f"{label} (planned only)" for label in labels}


def _rules_assumptions(draft: AnalysisDraft, deck: DeckDocument) -> list[Assumption]:
    items: list[Assumption] = []
    if draft.snapshot.sector.provided:
        items.append(Assumption(statement=f"Sector identified as {draft.snapshot.sector.value}.",
                                classification=Classification.INFERENCE, basis="Keyword frequency across slides."))
    if draft.snapshot.name.classification == Classification.ASSUMPTION:
        items.append(Assumption(statement=f"Company name taken from the file name ({draft.snapshot.name.value}).",
                                classification=Classification.ASSUMPTION, basis="No company name found on slide 1."))
    for finding in draft.findings:
        if finding.planned:
            items.append(Assumption(statement=f"{finding.statement} (S{', S'.join(map(str, finding.slides))}).",
                                    classification=Classification.INFERENCE,
                                    basis="Roadmap language; not credited as completed."))
    if draft.ai_data.applicable and draft.ai_data.proprietary_assessment.startswith("INFERENCE"):
        items.append(Assumption(statement=draft.ai_data.proprietary_assessment.removeprefix("INFERENCE: "),
                                classification=Classification.INFERENCE, basis="Dependency and data signals."))
    image_only = deck.image_only_slides()
    if image_only:
        items.append(Assumption(statement="Image-only slides " + ", ".join(map(str, image_only))
                                + " were not assessed (no OCR).", classification=Classification.ASSUMPTION))
    return items


def finalize(draft: AnalysisDraft, deck: DeckDocument, settings: Settings) -> DiligenceAnalysis:
    source = SourceDocument(filename=deck.filename, file_type=deck.file_type, slide_count=deck.slide_count,
                            sha256=deck.sha256, text_characters=deck.text_characters,
                            image_only_slides=deck.image_only_slides(), parse_warnings=list(deck.warnings))
    flags = sort_flags(draft.flags)
    flags += gap_flags(draft.missing, flags)
    scores = compute_scores(draft.findings, flags, settings.unverified_score, draft.proposed_scores)
    confidence = evidence_confidence(draft.findings, draft.snapshot, source, draft.grounding_rejections)
    scores = scores.model_copy(update={"uncertainty": uncertainty_band(confidence)})

    tech = technical_position(scores, flags)
    ip = ip_position(scores, flags)
    readiness = transaction_readiness(scores, flags, confidence)
    questions = build_questions(flags, scores, draft.questions)
    exit_readiness = _exit_readiness(flags, draft.exit_summary)
    name = draft.snapshot.display_name()
    summary = draft.conclusion_summary or _conclusion(name, tech, ip, readiness, scores, confidence, flags)

    return DiligenceAnalysis(
        source=source, engine=draft.engine, company=draft.snapshot, source_evidence=draft.findings,
        technical_architecture=draft.technical_architecture, development_maturity=draft.development_maturity,
        engineering_organization=draft.engineering_organization,
        technical_documentation=draft.technical_documentation, cybersecurity=draft.cybersecurity,
        intellectual_property=draft.intellectual_property, ai_data=draft.ai_data,
        third_party_dependencies=draft.dependencies, scalability=draft.scalability, exit_readiness=exit_readiness,
        scores=scores, evidence_confidence=confidence, red_flags=flags, diligence_questions=questions,
        assumptions=draft.assumptions, missing_information=draft.missing,
        investor_conclusion=InvestorConclusion(technical_position=tech, ip_position=ip,
                                               transaction_readiness=readiness, summary=summary),
        warnings=draft.warnings,
    )


def _exit_readiness(flags: list[RedFlag], summary: str | None) -> ExitReadinessAssessment:
    material = [f for f in flags if f.severity in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM)]
    exposures = [ExposureItem(issue=f.issue, severity=f.severity, exposure_types=f.exposures, flag_code=f.code)
                 for f in material]
    critical = [f for f in flags if f.severity == Severity.CRITICAL]
    high = [f for f in flags if f.severity == Severity.HIGH]
    if not summary:
        if critical:
            summary = (f"{len(critical)} issue(s) likely to become closing conditions, escrow or indemnity items: "
                       + "; ".join(_lower_first(f.issue) for f in critical[:3]) + ".")
        elif high:
            summary = ("No closing blockers identified from the deck; high-severity items likely to drive "
                       "diligence questions and reps & warranties: "
                       + "; ".join(_lower_first(f.issue) for f in high[:3]) + ".")
        else:
            summary = "No critical or high-severity transaction exposures identified from deck evidence."
    return ExitReadinessAssessment(summary=summary, exposures=exposures,
                                   finding_ids=[], gaps=[])


def _conclusion(name: str, tech, ip, readiness: TransactionReadiness, scores: ScoreSummary, confidence: int,
                flags: list[RedFlag]) -> str:
    critical = [f for f in flags if f.severity == Severity.CRITICAL]
    high = [f for f in flags if f.severity == Severity.HIGH]
    first = (f"On deck evidence, {shorten(name, 60)} rates {tech.value} on technology and {ip.value} on IP "
             f"(score {scores.overall_score} ± {scores.uncertainty}; evidence confidence {confidence}/100).")
    if critical:
        second = (f"{len(critical)} critical issue(s), led by {_lower_first(critical[0].issue)}, would likely surface "
                  "as closing conditions or escrow/indemnity items and should be resolved before a term sheet.")
    elif high:
        second = (f"No closing blockers surfaced, but {len(high)} high-severity item(s), led by "
                  f"{_lower_first(high[0].issue)}, require verification in diligence.")
    else:
        second = ("No critical or high-severity technical or IP issues surfaced; deck claims still require "
                  "verification against source code, IP agreements and security reports.")
    if confidence < 50:
        third = (f"Transaction readiness: {readiness.value}; limited disclosure means scores could move materially "
                 "once the data room is reviewed.")
    else:
        third = f"Transaction readiness: {readiness.value}."
    return " ".join([first, second, third])
