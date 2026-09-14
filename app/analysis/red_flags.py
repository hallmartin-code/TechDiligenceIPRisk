"""Red-flag templates, severity escalation, and absence-of-evidence flags.

A signal becomes a flag through a template that supplies the diligence framing
(consequence, question, mitigation, transaction exposure). Severity then moves
with context: contractor-built code is HIGH on its own but CRITICAL when no
invention assignments are evidenced, and MEDIUM when they are.

Flags derived from combinations are classified INFERENCE; flags raised because
expected evidence is missing are classified NOT PROVIDED. Neither is ever
presented as a fact.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.analysis.signals import DetectedSignal
from app.models.schemas import (
    Classification,
    MissingInformation,
    RedFlag,
    SEVERITY_RANK,
    ScoreCategory as C,
    Severity as S,
    TransactionExposure as X,
)
from app.utils.text import lower_first, shorten, slide_ref


@dataclass(frozen=True)
class FlagTemplate:
    category: C
    severity: S
    issue: str
    consequence: str
    question: str
    mitigation: str
    exposures: tuple[X, ...]
    audience: str


TEMPLATES: dict[str, FlagTemplate] = {
    "CONTRACTOR_DEVELOPED_CODE": FlagTemplate(
        C.IP_OWNERSHIP, S.HIGH, "Core code developed by contractors or an outsourced agency",
        "Without executed assignments the company may not own the code it sells; acquirers treat this as a chain-of-title defect.",
        "Which contractors or agencies wrote production code, and is a signed IP assignment in place for each?",
        "Collect or re-paper contractor IP assignments (nunc pro tunc where needed) and document contribution history.",
        (X.REPS_AND_WARRANTIES, X.CLOSING_CONDITION, X.INDEMNITY, X.ESCROW), "IP counsel"),
    "IP_OWNERSHIP_UNCLEAR": FlagTemplate(
        C.IP_OWNERSHIP, S.CRITICAL, "IP ownership or assignment described as unresolved",
        "Unresolved title to core IP can block financing or closing and push value into escrow or price adjustments.",
        "Which assignments or licenses remain outstanding, with whom, and when will they be executed?",
        "Complete assignments before a term sheet and obtain a chain-of-title review from IP counsel.",
        (X.CLOSING_CONDITION, X.REPS_AND_WARRANTIES, X.ESCROW, X.DELAY_OR_TERMINATION), "IP counsel"),
    "FOUNDER_HELD_IP": FlagTemplate(
        C.IP_OWNERSHIP, S.HIGH, "Core IP held personally by a founder rather than the company",
        "Founder-held IP sits outside the company and must be assigned before investment or sale.",
        "Has every founder assigned all pre-incorporation IP to the company, recorded with the USPTO where applicable?",
        "Execute and record founder IP assignments as a closing deliverable.",
        (X.CLOSING_CONDITION, X.REPS_AND_WARRANTIES), "IP counsel"),
    "UNIVERSITY_IP": FlagTemplate(
        C.IP_OWNERSHIP, S.HIGH, "Technology originated at or is licensed from a university",
        "University rights often carry field-of-use limits, royalties and consent-to-assign terms that affect exit value.",
        "Provide the university license or assignment: exclusivity, field of use, royalties, sublicensing and change-of-control terms.",
        "Review terms early; negotiate consent-to-assign or buy-out rights before a transaction.",
        (X.CLOSING_CONDITION, X.PURCHASE_PRICE_ADJUSTMENT, X.DELAY_OR_TERMINATION), "IP counsel"),
    "PRIOR_EMPLOYER_CLAIM": FlagTemplate(
        C.IP_OWNERSHIP, S.HIGH, "Core technology may have been conceived at a prior employer",
        "A former employer's invention-assignment agreement could support an ownership claim against core IP.",
        "When and where was the core technology conceived, and do founders' prior employment agreements claim it?",
        "Obtain prior-employer agreements and conception records; secure releases if needed.",
        (X.REPS_AND_WARRANTIES, X.INDEMNITY, X.ESCROW, X.DELAY_OR_TERMINATION), "IP counsel"),
    "GOVERNMENT_FUNDED_IP": FlagTemplate(
        C.IP_OWNERSHIP, S.MEDIUM, "Development funded by government grants or contracts",
        "Government funding can carry march-in rights, data-rights limits and invention-reporting duties that follow the IP.",
        "Which inventions were made under government funding, and were Bayh-Dole disclosure and election duties met?",
        "Audit grant agreements, invention reports and data-rights markings.",
        (X.DILIGENCE_QUESTION, X.REPS_AND_WARRANTIES), "IP counsel"),
    "JOINT_DEVELOPMENT_IP": FlagTemplate(
        C.IP_OWNERSHIP, S.HIGH, "IP jointly developed with, or owned by, a customer or partner",
        "Joint or customer ownership can restrict licensing and complicate assignment in an acquisition.",
        "Which agreements give customers or partners ownership, joint ownership or exclusive rights in developed IP?",
        "Inventory joint-development terms; renegotiate toward company ownership with licenses back.",
        (X.REPS_AND_WARRANTIES, X.INDEMNITY, X.PURCHASE_PRICE_ADJUSTMENT), "IP counsel"),
    "COPYLEFT_OPEN_SOURCE": FlagTemplate(
        C.IP_OWNERSHIP, S.HIGH, "Copyleft (GPL-family) open-source components referenced",
        "Copyleft terms can compel disclosure of proprietary source when distributed or network-served; a standard R&W and indemnity item.",
        "Which copyleft components are used, how are they linked or distributed, and is there a current license scan / SBOM?",
        "Run software composition analysis; replace or isolate copyleft components; adopt an OSS policy.",
        (X.REPS_AND_WARRANTIES, X.INDEMNITY, X.PURCHASE_PRICE_ADJUSTMENT), "CTO / IP counsel"),
    "OPEN_SOURCE_UNREVIEWED": FlagTemplate(
        C.IP_OWNERSHIP, S.MEDIUM, "Open-source usage without evidenced license compliance",
        "Unreviewed licenses are a routine but time-consuming diligence item and can surface remediation work.",
        "What percentage of the codebase is open source, and which licenses impose copyleft or redistribution obligations?",
        "Produce an SBOM and license scan (e.g., FOSSA, Black Duck, Snyk) before diligence.",
        (X.DILIGENCE_QUESTION, X.REPS_AND_WARRANTIES), "CTO"),
    "TRAINING_DATA_RIGHTS": FlagTemplate(
        C.IP_OWNERSHIP, S.HIGH, "Model training data sourced by scraping or unclear licenses",
        "Unclear data rights can impair model value, invite infringement or privacy claims, and force retraining.",
        "What data trained or tuned the models, under what license or consent, and could models be rebuilt without it?",
        "Build a data-provenance register; license or remove unlicensed data.",
        (X.REPS_AND_WARRANTIES, X.INDEMNITY, X.ESCROW), "IP counsel"),
    "CUSTOMER_DATA_RIGHTS": FlagTemplate(
        C.IP_OWNERSHIP, S.MEDIUM, "Customer data used to train or improve models",
        "Customer contracts may not permit cross-customer training; privacy obligations may apply.",
        "Do customer agreements expressly permit using their data to train models used for other customers?",
        "Update contracts and DPAs; add opt-in controls and data segregation.",
        (X.REPS_AND_WARRANTIES, X.DILIGENCE_QUESTION), "IP counsel"),
    "IP_ASSIGNMENT_NOT_EVIDENCED": FlagTemplate(
        C.IP_OWNERSHIP, S.MEDIUM, "Invention-assignment coverage not evidenced",
        "Missing employee or contractor assignments are among the most common IP closing defects.",
        "Have all current and former employees, founders and contractors executed invention-assignment agreements?",
        "Audit and collect signed invention-assignment agreements for every contributor.",
        (X.REPS_AND_WARRANTIES, X.CLOSING_CONDITION), "IP counsel"),
    "KEY_PERSON_DEPENDENCY": FlagTemplate(
        C.ENGINEERING_ORGANIZATION, S.HIGH, "Critical technical knowledge concentrated in one person",
        "Losing that person could stall development; acquirers require retention terms or discount value.",
        "Which systems could only be maintained by the founder or a single engineer today, and what is the succession plan?",
        "Document architecture and runbooks, add a second maintainer per critical system, use retention vesting.",
        (X.CLOSING_CONDITION, X.ESCROW, X.PURCHASE_PRICE_ADJUSTMENT), "Technical advisor"),
    "NO_TECHNICAL_LEADER": FlagTemplate(
        C.ENGINEERING_ORGANIZATION, S.HIGH, "No full-time CTO or engineering leader in place",
        "Architecture, security and scaling decisions lack accountable ownership through the next growth phase.",
        "Who owns architecture and engineering decisions today, and what is the timeline for the technical leadership hire?",
        "Hire or promote a full-time technical leader; retain an interim technical advisor until then.",
        (X.DILIGENCE_QUESTION, X.CLOSING_CONDITION), "Investor"),
    "FRACTIONAL_TECH_LEADERSHIP": FlagTemplate(
        C.ENGINEERING_ORGANIZATION, S.MEDIUM, "Technical leadership is fractional, interim or advisory",
        "Part-time leadership limits accountability for architecture and security through scale-up.",
        "How much time does the technical lead commit, and who controls system credentials and architecture knowledge?",
        "Transition to full-time leadership; ensure all system credentials are company-controlled.",
        (X.DILIGENCE_QUESTION,), "Investor"),
    "SMALL_ENGINEERING_TEAM": FlagTemplate(
        C.ENGINEERING_ORGANIZATION, S.MEDIUM, "Very small engineering team for later-stage expectations",
        "Limited capacity to maintain, secure and scale the product while delivering the roadmap.",
        "How is engineering capacity split between maintenance, security and roadmap, and what is the hiring plan?",
        "Tie the hiring plan to roadmap and support load; document critical systems.",
        (X.DILIGENCE_QUESTION,), "Technical advisor"),
    "TECH_LEADERSHIP_NOT_IDENTIFIED": FlagTemplate(
        C.ENGINEERING_ORGANIZATION, S.MEDIUM, "Technical leadership not identified",
        "Investors cannot assess who owns architecture, security and delivery.",
        "Who leads engineering, what is their background, and who else can operate the core systems?",
        "Present technical leadership and engineering org structure in the data room.",
        (X.DILIGENCE_QUESTION,), "Investor"),
    "UNDOCUMENTED_CODEBASE": FlagTemplate(
        C.TECHNICAL_DOCUMENTATION, S.HIGH, "Technical documentation limited or absent",
        "Undocumented systems slow technical diligence, raise integration cost and deepen key-person risk.",
        "What architecture, API and operational documentation exists, and could a new senior engineer run the system from it?",
        "Produce architecture diagrams, decision records, runbooks and onboarding docs before diligence.",
        (X.DILIGENCE_QUESTION, X.PURCHASE_PRICE_ADJUSTMENT), "CTO"),
    "TECH_DEBT": FlagTemplate(
        C.TECHNICAL_DOCUMENTATION, S.MEDIUM, "Material technical debt or planned rebuild disclosed",
        "A rebuild competes with roadmap spend and may be priced in as inherited technical debt.",
        "What technical debt would an acquirer inherit, what does remediation cost, and is it funded in this round?",
        "Scope and budget remediation with milestones; disclose it in the data room.",
        (X.DILIGENCE_QUESTION, X.PURCHASE_PRICE_ADJUSTMENT), "Technical advisor"),
    "LEGACY_ARCHITECTURE": FlagTemplate(
        C.TECHNICAL_ARCHITECTURE, S.MEDIUM, "Legacy components in the production stack",
        "Legacy components raise security patching and hiring cost and may need replacement at scale.",
        "Which production components are legacy or unsupported, and what is the migration plan and cost?",
        "Plan and fund migration off unsupported components.",
        (X.DILIGENCE_QUESTION, X.PURCHASE_PRICE_ADJUSTMENT), "CTO"),
    "SINGLE_POINT_OF_FAILURE": FlagTemplate(
        C.TECHNICAL_ARCHITECTURE, S.HIGH, "Single point of failure in production infrastructure",
        "One outage or data-loss event could breach customer commitments; a standard enterprise and acquirer concern.",
        "What happens to customers if the primary server or database fails, and what are the tested RPO and RTO?",
        "Add redundancy, automated backups and tested failover; publish RPO/RTO.",
        (X.DILIGENCE_QUESTION, X.REPS_AND_WARRANTIES), "CTO / CISO"),
    "FRAGILE_INTEGRATIONS": FlagTemplate(
        C.TECHNICAL_ARCHITECTURE, S.MEDIUM, "Manual or per-customer custom integrations",
        "Custom integration work raises cost to serve and slows onboarding as customers grow.",
        "What share of implementation effort is custom per customer, and what is the path to productized connectors?",
        "Productize integrations and APIs; track implementation hours per customer.",
        (X.DILIGENCE_QUESTION,), "Technical advisor"),
    "CUSTOMER_CODE_FORKS": FlagTemplate(
        C.TECHNICAL_ARCHITECTURE, S.HIGH, "Customer-specific code branches or builds",
        "Parallel versions multiply maintenance and patching cost; acquirers treat them as inherited technical debt.",
        "How many customer-specific branches or builds exist, and what is the plan to converge on one codebase?",
        "Converge to a single multi-tenant codebase with configuration flags.",
        (X.DILIGENCE_QUESTION, X.PURCHASE_PRICE_ADJUSTMENT), "CTO"),
    "ARCHITECTURE_NOT_DESCRIBED": FlagTemplate(
        C.TECHNICAL_ARCHITECTURE, S.MEDIUM, "Technical architecture not described",
        "Scalability, resilience and technical-debt claims cannot be assessed without architecture detail.",
        "Can you provide an architecture diagram covering services, data stores, hosting, integrations and single points of failure?",
        "Prepare architecture and infrastructure documentation for the data room.",
        (X.DILIGENCE_QUESTION,), "Technical advisor"),
    "PROTOTYPE_STAGE": FlagTemplate(
        C.DEVELOPMENT_MATURITY, S.MEDIUM, "Product described at prototype / MVP stage",
        "Valuation may rest on development not yet completed or proven under production load.",
        "Which capabilities run in production with paying customers today, and which are still roadmap?",
        "Tie funding milestones to production-readiness criteria.",
        (X.DILIGENCE_QUESTION,), "Technical advisor"),
    "NO_QA_PROCESS": FlagTemplate(
        C.DEVELOPMENT_MATURITY, S.MEDIUM, "Testing and QA limited or manual",
        "Regressions and outages become more likely as release velocity and customer count grow.",
        "What automated test coverage exists, and how are releases validated before reaching production?",
        "Add CI with automated tests and release gates.",
        (X.DILIGENCE_QUESTION,), "CTO"),
    "SCALABILITY_BOTTLENECK": FlagTemplate(
        C.SCALABILITY, S.HIGH, "Scaling bottleneck or re-platforming need disclosed",
        "Growth could require re-platforming spend before revenue scales.",
        "What technical bottleneck appears first at 10x current volume, and what would it cost to remove?",
        "Load-test to 10x volume; budget re-architecture of the constraining component.",
        (X.DILIGENCE_QUESTION, X.PURCHASE_PRICE_ADJUSTMENT), "Technical advisor"),
    "SERVICES_HEAVY_DELIVERY": FlagTemplate(
        C.SCALABILITY, S.MEDIUM, "Services-heavy implementation or manual delivery",
        "Revenue growth may need proportional headcount, compressing gross margin.",
        "How many implementation and support hours does each new customer require, and how is that trending?",
        "Automate onboarding; report services revenue and margin separately.",
        (X.DILIGENCE_QUESTION, X.PURCHASE_PRICE_ADJUSTMENT), "Investor"),
    "INFERENCE_COST_EXPOSURE": FlagTemplate(
        C.SCALABILITY, S.MEDIUM, "Variable model-inference or API cost exposure",
        "Usage growth can raise cost of revenue faster than revenue if pricing is not usage-indexed.",
        "What is inference or API cost per customer today, and how does gross margin change at 10x usage?",
        "Track unit cost; add caching, model routing and usage-based pricing.",
        (X.DILIGENCE_QUESTION, X.PURCHASE_PRICE_ADJUSTMENT), "Investor"),
    "LOW_GROSS_MARGIN": FlagTemplate(
        C.SCALABILITY, S.MEDIUM, "Gross margin below software benchmarks",
        "Low margin signals hosting, services or hardware cost to serve that limits valuation multiples.",
        "What are the components of cost of revenue, and which technical changes improve margin at scale?",
        "Break out COGS by hosting, services, third-party APIs and hardware; set a margin roadmap.",
        (X.PURCHASE_PRICE_ADJUSTMENT, X.DILIGENCE_QUESTION), "Investor"),
    "FOUNDATION_MODEL_DEPENDENCY": FlagTemplate(
        C.THIRD_PARTY_DEPENDENCY, S.HIGH, "Core product built on third-party foundation-model APIs",
        "Provider pricing, policy or model changes can impair margin and differentiation; the AI advantage may not be proprietary.",
        "What is proprietary beyond the third-party model, and how quickly could you switch providers?",
        "Build a model-agnostic layer, evaluate alternative providers and invest in proprietary data and evaluations.",
        (X.DILIGENCE_QUESTION, X.PURCHASE_PRICE_ADJUSTMENT), "CTO / Acquirer"),
    "SINGLE_CLOUD_DEPENDENCY": FlagTemplate(
        C.THIRD_PARTY_DEPENDENCY, S.LOW, "Exclusive reliance on one cloud provider",
        "Common and usually acceptable; matters mainly to acquirers with competing cloud commitments.",
        "Which cloud-proprietary services would be hardest to replace?",
        "Document portability constraints and avoid unnecessary proprietary lock-in.",
        (X.DILIGENCE_QUESTION,), "CTO"),
    "SUPPLIER_CONCENTRATION": FlagTemplate(
        C.THIRD_PARTY_DEPENDENCY, S.HIGH, "Single-source supplier or manufacturer",
        "Supply disruption or price changes directly affect delivery and margin.",
        "Which components are single-sourced, and what would qualifying a second source cost and take?",
        "Qualify second sources; secure supply agreements with continuity terms.",
        (X.DILIGENCE_QUESTION, X.REPS_AND_WARRANTIES), "Acquirer"),
    "LICENSED_CORE_TECHNOLOGY": FlagTemplate(
        C.THIRD_PARTY_DEPENDENCY, S.HIGH, "Core technology licensed from a third party",
        "Licensed core technology limits defensibility and may not transfer on acquisition.",
        "What are the term, exclusivity, royalty, termination and assignment provisions of the core technology license?",
        "Negotiate long-term assignable rights or build an owned replacement.",
        (X.CLOSING_CONDITION, X.PURCHASE_PRICE_ADJUSTMENT), "IP counsel"),
    "CHANGE_OF_CONTROL_RESTRICTION": FlagTemplate(
        C.THIRD_PARTY_DEPENDENCY, S.HIGH, "Change-of-control or anti-assignment restrictions referenced",
        "Third-party consents may be required to close, giving counterparties leverage over the deal.",
        "Which contracts or licenses require consent on change of control or assignment?",
        "Inventory consent requirements and pre-negotiate waivers.",
        (X.CLOSING_CONDITION, X.DELAY_OR_TERMINATION), "IP counsel / Acquirer"),
    "NO_SECURITY_PROGRAM": FlagTemplate(
        C.CYBERSECURITY_COMPLIANCE, S.HIGH, "No formal security program or attestation",
        "Enterprise buyers and acquirers expect SOC 2 / ISO 27001-level controls; absence delays sales and closing.",
        "What security controls, penetration tests and attestations exist today, and what is the SOC 2 timeline?",
        "Implement baseline controls, commission a penetration test and start a SOC 2 program.",
        (X.DILIGENCE_QUESTION, X.REPS_AND_WARRANTIES, X.INDEMNITY), "CISO"),
    "SECURITY_INCIDENT": FlagTemplate(
        C.CYBERSECURITY_COMPLIANCE, S.CRITICAL, "Prior security incident disclosed",
        "Incident history can carry notification liability, customer claims and specific indemnities.",
        "What happened, what data was affected, what notifications were made, and what remediation was verified?",
        "Provide the incident report, forensic findings and remediation evidence.",
        (X.REPS_AND_WARRANTIES, X.INDEMNITY, X.ESCROW), "CISO"),
    "SECURITY_NOT_EVIDENCED": FlagTemplate(
        C.CYBERSECURITY_COMPLIANCE, S.MEDIUM, "Security and compliance posture not described",
        "Security diligence starts from zero; undocumented controls can delay enterprise sales and closing.",
        "What security attestations (e.g., SOC 2 Type II), penetration tests and incident-response processes are current?",
        "Prepare a security overview, policies and the latest penetration test for the data room.",
        (X.DILIGENCE_QUESTION,), "CISO"),
    "NO_PATENT_PROTECTION": FlagTemplate(
        C.IP_DEFENSIBILITY, S.MEDIUM, "No filed patents; filings only planned",
        "Defensibility rests on trade secrets and execution; delayed filing risks public-disclosure bars.",
        "Which inventions are filing candidates, and have any been publicly disclosed in papers, demos or sales material?",
        "File provisional applications on core inventions promptly; document trade-secret controls.",
        (X.DILIGENCE_QUESTION,), "IP counsel"),
    "PATENT_POSITION_NOT_PROVIDED": FlagTemplate(
        C.IP_DEFENSIBILITY, S.MEDIUM, "Patent and trade-secret position not described",
        "Defensibility cannot be verified; investors will assume no registered IP.",
        "Which patents or trade secrets directly protect the revenue-generating product?",
        "Prepare an IP schedule mapping patents and trade secrets to products.",
        (X.DILIGENCE_QUESTION,), "IP counsel"),
    "COMMODITY_TECHNOLOGY": FlagTemplate(
        C.IP_DEFENSIBILITY, S.HIGH, "Technology described as a thin layer over off-the-shelf components",
        "A low technical barrier to replication reduces strategic value.",
        "What would a well-funded competitor need to replicate the product, and how long would it take?",
        "Invest in proprietary data, workflow depth or patentable methods.",
        (X.DILIGENCE_QUESTION, X.PURCHASE_PRICE_ADJUSTMENT), "Acquirer"),
    "EVIDENCE_GAP": FlagTemplate(
        C.EXIT_READINESS, S.LOW, "Expected diligence evidence not in the deck",
        "Minor gap; expect a documentation request during diligence.",
        "Can you provide documentation for this item?",
        "Add supporting documentation to the data room.",
        (X.DILIGENCE_QUESTION,), "Investor"),
    "CUSTOMER_CONCENTRATION": FlagTemplate(
        C.EXIT_READINESS, S.MEDIUM, "High customer concentration",
        "Dependence on one customer drives customer-specific code and transaction risk if it churns or holds consent rights.",
        "What share of revenue and roadmap does the largest customer drive, and do its contracts include change-of-control terms?",
        "Diversify revenue; review key-customer contract terms.",
        (X.DILIGENCE_QUESTION, X.REPS_AND_WARRANTIES), "Investor"),
}

# Codes the LLM engine may emit; "OTHER" covers issues outside the catalog.
FLAG_CODES: tuple[str, ...] = tuple(TEMPLATES) + ("OTHER",)


def audience_for(code: str) -> str:
    template = TEMPLATES.get(code)
    return template.audience if template else "Investor"


def _flag(code: str, signal: DetectedSignal | None = None, *, severity: S | None = None,
          issue: str | None = None, classification: Classification = Classification.FACT,
          evidence: str | None = None, slides: list[int] | None = None) -> RedFlag:
    template = TEMPLATES[code]
    if signal is not None:
        slides = slides if slides is not None else list(signal.slides)
        evidence = evidence or f'{slide_ref(signal.slides)}: "{shorten(signal.excerpt, 160)}"'
    return RedFlag(
        code=code, category=template.category, issue=issue or template.issue,
        severity=severity or template.severity, classification=classification,
        evidence=evidence or "Not addressed anywhere in the deck.", slides=slides or [],
        business_consequence=template.consequence, diligence_question=template.question,
        mitigation=template.mitigation, exposures=list(template.exposures))


def derive_flags(signals: dict[str, DetectedSignal], regulated_or_enterprise: bool) -> list[RedFlag]:
    active = {key: s for key, s in signals.items() if not s.planned}

    def has(*keys: str) -> bool:
        return any(k in active for k in keys)

    flags: dict[str, RedFlag] = {}
    for signal in active.values():
        code = signal.effective_flag
        if code and code not in flags:
            flags[code] = _flag(code, signal)

    def adjust(code: str, **changes) -> None:
        if code in flags:
            flags[code] = flags[code].model_copy(update=changes)

    if "CONTRACTOR_DEVELOPED_CODE" in flags:
        if has("ipo_assignment"):
            adjust("CONTRACTOR_DEVELOPED_CODE", severity=S.MEDIUM)
        else:
            adjust("CONTRACTOR_DEVELOPED_CODE", severity=S.CRITICAL,
                   issue="Contractor-built core code with no evidenced IP assignments",
                   classification=Classification.INFERENCE)
    if "KEY_PERSON_DEPENDENCY" in flags and has("td_undocumented"):
        adjust("KEY_PERSON_DEPENDENCY", severity=S.CRITICAL, classification=Classification.INFERENCE,
               issue="Core system knowledge held by one person with little documentation")
    if "UNIVERSITY_IP" in flags and has("ipo_ownership_unclear", "ipo_founder_personal"):
        adjust("UNIVERSITY_IP", severity=S.CRITICAL, classification=Classification.INFERENCE,
               issue="University-originated technology with unresolved assignment or license")
    if "PRIOR_EMPLOYER_CLAIM" in flags and not has("ipo_assignment", "ipo_company_owned"):
        adjust("PRIOR_EMPLOYER_CLAIM", severity=S.CRITICAL, classification=Classification.INFERENCE)
    if "FOUNDER_HELD_IP" in flags and has("ipo_ownership_unclear"):
        adjust("FOUNDER_HELD_IP", severity=S.CRITICAL, classification=Classification.INFERENCE)
    if "COPYLEFT_OPEN_SOURCE" in flags and has("ipo_oss_compliance"):
        adjust("COPYLEFT_OPEN_SOURCE", severity=S.MEDIUM)
    if "OPEN_SOURCE_UNREVIEWED" in flags and (has("ipo_oss_compliance") or "COPYLEFT_OPEN_SOURCE" in flags):
        flags.pop("OPEN_SOURCE_UNREVIEWED")
    if "FOUNDATION_MODEL_DEPENDENCY" in flags and has("tpd_multi_vendor", "ipd_proprietary_data", "ai_fine_tuned"):
        adjust("FOUNDATION_MODEL_DEPENDENCY", severity=S.MEDIUM)
    if "NO_SECURITY_PROGRAM" in flags and not regulated_or_enterprise:
        adjust("NO_SECURITY_PROGRAM", severity=S.MEDIUM)
    if "NO_TECHNICAL_LEADER" in flags and has("eo_cto"):
        flags.pop("NO_TECHNICAL_LEADER")
    if "PROTOTYPE_STAGE" in flags and has("dm_production", "dm_scaled"):
        flags.pop("PROTOTYPE_STAGE")
    if "SMALL_ENGINEERING_TEAM" in flags and "KEY_PERSON_DEPENDENCY" in flags:
        flags.pop("SMALL_ENGINEERING_TEAM")

    # Absence-of-evidence flags: classified NOT PROVIDED, never as facts.
    def absent(code: str, keys: tuple[str, ...], severity: S | None = None, evidence: str | None = None) -> None:
        if not has(*keys) and code not in flags:
            flags[code] = _flag(code, severity=severity, classification=Classification.NOT_PROVIDED,
                                evidence=evidence)

    if "CONTRACTOR_DEVELOPED_CODE" not in flags and "IP_OWNERSHIP_UNCLEAR" not in flags:
        absent("IP_ASSIGNMENT_NOT_EVIDENCED", ("ipo_assignment",))
    security_keys = ("cy_soc2", "cy_iso27001", "cy_hipaa", "cy_gdpr", "cy_pci", "cy_fedramp", "cy_fda_cyber",
                     "cy_pentest", "cy_encryption", "cy_access", "cy_no_security", "cy_incident")
    planned_security = [s for k, s in signals.items() if k in security_keys and s.planned]
    absent("SECURITY_NOT_EVIDENCED", security_keys,
           severity=S.HIGH if regulated_or_enterprise else S.MEDIUM,
           evidence=(f"Only planned: {planned_security[0].rule.label} ({slide_ref(planned_security[0].slides)})."
                     if planned_security else None))
    absent("ARCHITECTURE_NOT_DESCRIBED", ("ta_architecture_slide", "ta_microservices", "ta_cloud_native",
                                          "ta_monolith", "ta_single_server", "ta_multiregion", "ta_api"))
    if "NO_PATENT_PROTECTION" not in flags:
        absent("PATENT_POSITION_NOT_PROVIDED", ("ipd_issued_patents", "ipd_pending_patents", "ipd_trade_secrets",
                                                "ipd_patent_plan"))
    if "NO_TECHNICAL_LEADER" not in flags:
        absent("TECH_LEADERSHIP_NOT_IDENTIFIED", ("eo_cto", "eo_technical_founder", "eo_fractional_cto",
                                                  "eo_cto_missing", "eo_founder_dependency"))
    return sort_flags(list(flags.values()))


def gap_flags(missing: list[MissingInformation], existing: list[RedFlag], minimum: int = 3) -> list[RedFlag]:
    """Top the risk list up to `minimum` with LOW evidence gaps, so a clean deck still shows what to verify."""
    needed = minimum - len(existing)
    if needed <= 0:
        return []
    template = TEMPLATES["EVIDENCE_GAP"]
    flags: list[RedFlag] = []
    for item in missing:
        if item.category == C.EXIT_READINESS:  # business metrics, not technical/IP evidence
            continue
        flags.append(RedFlag(
            code="EVIDENCE_GAP", category=item.category, issue=f"{item.item} not evidenced in the deck",
            severity=S.LOW, classification=Classification.NOT_PROVIDED, evidence="Not addressed anywhere in the deck.",
            slides=[], business_consequence=template.consequence,
            diligence_question=f"Can you provide documentation of {lower_first(item.item)}?",
            mitigation=template.mitigation, exposures=list(template.exposures)))
        if len(flags) >= needed:
            break
    return flags


def sort_flags(flags: list[RedFlag]) -> list[RedFlag]:
    from app.analysis.risk_scoring import DEFAULT_WEIGHTS

    return sorted(flags, key=lambda f: (SEVERITY_RANK[f.severity],
                                        {Classification.FACT: 0, Classification.INFERENCE: 1}.get(f.classification, 2),
                                        -DEFAULT_WEIGHTS.get(f.category, 0)))
