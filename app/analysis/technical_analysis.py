"""Technical sections of the rules engine: architecture, maturity, engineering,
documentation, cybersecurity and scalability.

Each section lists what a later-stage technical diligence expects to see; any
expectation with no supporting deck evidence becomes an explicit NOT PROVIDED
gap rather than being silently skipped.
"""

from __future__ import annotations

import re

from app.ingestion.document import DeckDocument
from app.models.schemas import (
    NOT_PROVIDED,
    CompanySnapshot,
    DevelopmentMaturityAssessment,
    EngineeringOrganizationAssessment,
    Finding,
    MaturityLevel,
    Polarity,
    ScalabilityAssessment,
    ScoreCategory as C,
    SectionAssessment,
    TechnicalArchitectureAssessment,
)
from app.utils.text import lower_first, slide_ref

EXPECTATIONS: dict[C, list[tuple[str, set[str]]]] = {
    C.TECHNICAL_ARCHITECTURE: [
        ("Architecture description", {"ta_architecture_slide", "ta_microservices", "ta_monolith", "ta_cloud_native"}),
        ("Hosting / deployment model", {"ta_cloud_native", "ta_multiregion", "ta_single_server", "ta_iac", "tpd_single_cloud"}),
        ("Redundancy and failover", {"ta_multiregion", "ta_single_server"}),
        ("Monitoring / observability", {"ta_observability"}),
        ("APIs and integration approach", {"ta_api", "ta_manual_integrations", "ta_customer_forks"}),
    ],
    C.DEVELOPMENT_MATURITY: [
        ("Production status", {"dm_production", "dm_prototype", "dm_beta_pilot", "dm_scaled"}),
        ("CI/CD and release process", {"dm_cicd", "dm_release"}),
        ("Automated testing / QA", {"dm_testing", "dm_no_testing"}),
        ("Uptime / reliability metrics", {"dm_uptime"}),
        ("Disaster recovery", {"dm_dr"}),
        ("Incident management", {"dm_incident"}),
    ],
    C.SCALABILITY: [
        ("Scaling approach", {"sc_autoscale", "sc_bottleneck", "ta_multiregion"}),
        ("Load or capacity evidence", {"sc_load_tested", "dm_scaled"}),
        ("Cost to serve / gross margin", {"sc_gross_margin", "sc_inference_cost", "sc_manual_services", "sc_hardware_cost"}),
    ],
    C.ENGINEERING_ORGANIZATION: [
        ("Technical leadership (CTO / VP Engineering)", {"eo_cto", "eo_cto_missing", "eo_fractional_cto", "eo_technical_founder"}),
        ("Engineering team size", {"eo_team_size"}),
        ("In-house vs. contractor development", {"eo_inhouse", "eo_contractors"}),
        ("Key-person dependency / succession", {"eo_founder_dependency", "td_docs"}),
    ],
    C.TECHNICAL_DOCUMENTATION: [
        ("Technical documentation", {"td_docs", "td_undocumented"}),
        ("Code review / quality controls", {"td_code_review"}),
        ("Technical debt disclosure", {"td_tech_debt"}),
    ],
    C.CYBERSECURITY_COMPLIANCE: [
        ("Security attestation (SOC 2 / ISO 27001)", {"cy_soc2", "cy_iso27001", "cy_fedramp", "cy_no_security"}),
        ("Penetration testing", {"cy_pentest"}),
        ("Encryption and access control", {"cy_encryption", "cy_access"}),
        ("Backups / disaster recovery", {"dm_dr"}),
        ("Incident response", {"dm_incident"}),
    ],
    C.IP_OWNERSHIP: [
        ("Invention-assignment agreements", {"ipo_assignment"}),
        ("Company ownership of core IP", {"ipo_company_owned", "ipo_founder_personal", "ipo_ownership_unclear"}),
        ("University / government / prior-employer rights", {"ipo_university", "ipo_gov_funding", "ipo_prior_employer",
                                                             "ipo_company_owned"}),
        ("Open-source license compliance", {"ipo_oss_compliance", "ipo_copyleft", "ipo_open_source_general"}),
    ],
    C.IP_DEFENSIBILITY: [
        ("Patent position", {"ipd_issued_patents", "ipd_pending_patents", "ipd_patent_plan"}),
        ("Trade secrets and controls", {"ipd_trade_secrets", "ipd_secret_controls"}),
        ("Proprietary data or algorithms", {"ipd_proprietary_data", "ipd_proprietary_algo", "ai_fine_tuned"}),
        ("Freedom-to-operate evidence", {"ex_audit"}),
    ],
    C.THIRD_PARTY_DEPENDENCY: [
        ("Critical third-party dependencies", {"tpd_foundation_model", "tpd_single_cloud", "tpd_sole_supplier",
                                               "tpd_licensed_core", "tpd_multi_vendor"}),
        ("License / change-of-control terms", {"tpd_change_of_control", "tpd_licensed_core"}),
    ],
}

_FRAMEWORKS: tuple[tuple[str, str], ...] = (
    ("SOC 2", r"\b(?:enterprise|b2b|saas|customers?)\b"),
    ("HIPAA", r"\b(?:patients?|phi|hospitals?|health systems?|clinical|healthcare|ehr)\b"),
    ("PCI DSS", r"\b(?:payments?|card(?:holder)? data|merchants?)\b"),
    ("FedRAMP / CMMC", r"\b(?:federal|government|dod|defen[cs]e|public sector)\b"),
    ("GDPR", r"\b(?:gdpr|europe(?:an)?|eu customers|uk customers|international)\b"),
    ("FDA premarket cybersecurity", r"\b(?:fda|510\(k\)|medical device|samd)\b"),
)
_FRAMEWORK_KEYS = {"SOC 2": {"cy_soc2", "cy_iso27001"}, "HIPAA": {"cy_hipaa"}, "PCI DSS": {"cy_pci"},
                   "FedRAMP / CMMC": {"cy_fedramp"}, "GDPR": {"cy_gdpr"}, "FDA premarket cybersecurity": {"cy_fda_cyber"}}


def in_category(findings: list[Finding], category: C) -> list[Finding]:
    return [f for f in findings if f.category == category or category in f.secondary_categories]


def gaps_for(category: C, findings: list[Finding]) -> list[str]:
    gaps = []
    for label, keys in EXPECTATIONS.get(category, []):
        matched = [f for f in findings if f.signal_key in keys]
        if any(not f.planned for f in matched):
            continue
        gaps.append(label + (" (planned only)" if matched else ""))
    return gaps


def _phrase(finding: Finding) -> str:
    text = lower_first(finding.statement.removeprefix("Planned, not completed: "))
    ref = slide_ref(finding.slides, 2)
    return f"{text} ({ref})" if ref else text


def compose_summary(findings: list[Finding], gaps: list[str], pos_limit: int = 3, neg_limit: int = 3,
                    gap_limit: int = 2) -> str:
    positives = sorted((f for f in findings if f.polarity == Polarity.POSITIVE and not f.planned),
                       key=lambda f: -f.weight)
    negatives = sorted((f for f in findings if f.polarity == Polarity.NEGATIVE and not f.planned),
                       key=lambda f: -f.weight)
    planned = [f for f in findings if f.planned]
    parts = []
    if negatives:
        parts.append("Concerns: " + "; ".join(_phrase(f) for f in negatives[:neg_limit]))
    if positives:
        parts.append("Evidence: " + "; ".join(_phrase(f) for f in positives[:pos_limit]))
    if planned:
        parts.append("Planned only: " + "; ".join(_phrase(f) for f in planned[:2]))
    if gaps:
        parts.append("Not provided: " + "; ".join(lower_first(g) for g in gaps[:gap_limit]))
    return ". ".join(parts) + "." if parts else NOT_PROVIDED


def assess_architecture(findings: list[Finding], deck: DeckDocument) -> TechnicalArchitectureAssessment:
    relevant = in_category(findings, C.TECHNICAL_ARCHITECTURE)
    gaps = gaps_for(C.TECHNICAL_ARCHITECTURE, findings)
    risk_keys = {"ta_single_server", "ta_legacy", "ta_customer_forks", "ta_manual_integrations", "ta_monolith",
                 "tpd_single_cloud", "tpd_foundation_model"}
    risk_points = [_phrase(f) for f in findings if f.signal_key in risk_keys and not f.planned]
    arch_slides = [f.slides for f in relevant if f.signal_key == "ta_architecture_slide"]
    for slides in arch_slides:
        for number in slides:
            slide = deck.slide(number)
            if slide and slide.image_count and len(slide.full_text) < 160:
                gaps.append(f"Architecture diagram on S{number} is an image; contents not machine-readable")
    return TechnicalArchitectureAssessment(
        summary=compose_summary(relevant, gaps), finding_ids=[f.id for f in relevant], gaps=gaps,
        risk_points=risk_points)


def classify_maturity(findings: list[Finding], snapshot: CompanySnapshot) -> tuple[MaturityLevel, str]:
    active = {f.signal_key: f for f in findings if not f.planned and f.signal_key}
    keys = set(active)
    team = 0
    if snapshot.engineering_team.provided:
        match = re.search(r"\d+", snapshot.engineering_team.value)
        team = int(match.group()) if match else 0
    production = "dm_production" in keys or ("dm_revenue" in keys and snapshot.customers.provided
                                             and "dm_prototype" not in keys and "dm_beta_pilot" not in keys)
    scaled = bool({"dm_scaled", "dm_uptime"} & keys)
    engineering = bool({"dm_cicd", "dm_testing"} & keys)
    enterprise = (bool({"cy_soc2", "cy_iso27001", "cy_fedramp"} & keys) and "dm_dr" in keys
                  and "dm_incident" in keys and team >= 15)
    prototype, pilot = "dm_prototype" in keys, "dm_beta_pilot" in keys
    has_revenue = "dm_revenue" in keys

    if not any([production, scaled, prototype, pilot, engineering]):
        return MaturityLevel.NOT_DETERMINABLE, "No production-status, release or reliability evidence in the deck."
    if production and scaled and engineering and enterprise:
        level = MaturityLevel.ENTERPRISE_GRADE
    elif production and scaled and engineering:
        level = MaturityLevel.SCALED_PRODUCTION
    elif production:
        level = MaturityLevel.PRODUCTION
    elif prototype and not has_revenue and not pilot:
        level = MaturityLevel.PROTOTYPE
    elif prototype or pilot:
        level = MaturityLevel.EARLY_PRODUCTION if has_revenue else MaturityLevel.PROTOTYPE
    else:
        level = MaturityLevel.NOT_DETERMINABLE

    order = ["dm_production", "dm_scaled", "dm_uptime", "dm_cicd", "dm_testing", "cy_soc2", "dm_dr",
             "dm_prototype", "dm_beta_pilot", "dm_revenue"]
    evidence = [_phrase(active[k]) for k in order if k in active][:4]
    rationale = f"{level.value}: " + ("; ".join(evidence) if evidence else "limited evidence")
    if any(f.planned for f in findings if f.category == C.DEVELOPMENT_MATURITY):
        rationale += ". Roadmap items not counted as completed"
    return level, rationale + "."


def assess_maturity(findings: list[Finding], snapshot: CompanySnapshot) -> DevelopmentMaturityAssessment:
    relevant = in_category(findings, C.DEVELOPMENT_MATURITY)
    gaps = gaps_for(C.DEVELOPMENT_MATURITY, findings)
    level, rationale = classify_maturity(findings, snapshot)
    return DevelopmentMaturityAssessment(summary=compose_summary(relevant, gaps), finding_ids=[f.id for f in relevant],
                                         gaps=gaps, level=level, rationale=rationale)


def assess_engineering(findings: list[Finding]) -> EngineeringOrganizationAssessment:
    relevant = in_category(findings, C.ENGINEERING_ORGANIZATION)
    gaps = gaps_for(C.ENGINEERING_ORGANIZATION, findings)
    key_person = any(f.signal_key == "eo_founder_dependency" and not f.planned for f in findings)
    return EngineeringOrganizationAssessment(summary=compose_summary(relevant, gaps),
                                             finding_ids=[f.id for f in relevant], gaps=gaps,
                                             key_person_risk=key_person)


def assess_documentation(findings: list[Finding]) -> SectionAssessment:
    relevant = in_category(findings, C.TECHNICAL_DOCUMENTATION)
    gaps = gaps_for(C.TECHNICAL_DOCUMENTATION, findings)
    return SectionAssessment(summary=compose_summary(relevant, gaps), finding_ids=[f.id for f in relevant], gaps=gaps)


def relevant_frameworks(deck: DeckDocument) -> list[str]:
    text = deck.full_text
    return [name for name, pattern in _FRAMEWORKS if len(re.findall(pattern, text, re.IGNORECASE)) >= 2]


def assess_cybersecurity(findings: list[Finding], deck: DeckDocument) -> SectionAssessment:
    relevant = in_category(findings, C.CYBERSECURITY_COMPLIANCE)
    gaps = gaps_for(C.CYBERSECURITY_COMPLIANCE, findings)
    active = {f.signal_key for f in findings if not f.planned}
    frameworks = relevant_frameworks(deck)
    for name in frameworks:
        if name != "SOC 2" and not (_FRAMEWORK_KEYS[name] & active):
            gaps.append(f"{name} compliance evidence")
    summary = compose_summary(relevant, gaps)
    if frameworks:
        summary = f"{summary} Relevant frameworks for this sector: {', '.join(frameworks)}."
    return SectionAssessment(summary=summary, finding_ids=[f.id for f in relevant], gaps=gaps)


def assess_scalability(findings: list[Finding]) -> ScalabilityAssessment:
    relevant = in_category(findings, C.SCALABILITY)
    gaps = gaps_for(C.SCALABILITY, findings)
    economic_keys = {"sc_inference_cost", "sc_manual_services", "sc_hardware_cost", "sc_gross_margin",
                     "ta_manual_integrations", "ta_customer_forks", "tpd_foundation_model"}
    economic = [_phrase(f) for f in findings
                if f.signal_key in economic_keys and f.polarity == Polarity.NEGATIVE and not f.planned]
    return ScalabilityAssessment(summary=compose_summary(relevant, gaps), finding_ids=[f.id for f in relevant],
                                 gaps=gaps, economic_risks=economic)
