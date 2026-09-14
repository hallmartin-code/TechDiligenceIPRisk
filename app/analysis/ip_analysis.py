"""Intellectual-property and AI/data sections of the rules engine.

Two reliability rules are applied in the wording itself: the existence of a
patent is never described as proof of defensibility, and no freedom-to-operate
conclusion is drawn unless the deck states an FTO analysis was obtained.
"""

from __future__ import annotations

from app.analysis.signals import is_ai_company
from app.analysis.technical_analysis import compose_summary, gaps_for, in_category
from app.ingestion.document import DeckDocument
from app.models.schemas import (
    NOT_PROVIDED,
    AIDataAssessment,
    Finding,
    IntellectualPropertyAssessment,
    Polarity,
    ScoreCategory as C,
    ThirdPartyDependency,
    Topic,
)
from app.utils.text import slide_ref


def _by_key(findings: list[Finding]) -> dict[str, Finding]:
    return {f.signal_key: f for f in findings if f.signal_key and not f.planned}


def _item(f: Finding) -> str:
    ref = slide_ref(f.slides, 2)
    return f"{f.statement} ({ref})" if ref else f.statement


def _join(items: list[str]) -> str:
    return "; ".join(items)


def assess_ip(findings: list[Finding], dependencies: list[ThirdPartyDependency]) -> IntellectualPropertyAssessment:
    keys = _by_key(findings)

    patent_parts = [_item(keys[k]) for k in ("ipd_issued_patents", "ipd_pending_patents") if k in keys]
    if patent_parts:
        patents = _join(patent_parts) + ". Claim coverage of the revenue product, ownership records and expiry not evidenced."
    elif "ipd_patent_plan" in keys:
        patents = _item(keys["ipd_patent_plan"]) + "; no filed or issued patents identified."
    else:
        patents = f"{NOT_PROVIDED}: no issued, pending or provisional patents identified in the deck."

    secret_parts = [_item(keys[k]) for k in ("ipd_trade_secrets", "ipd_proprietary_algo") if k in keys]
    if secret_parts:
        trade_secrets = _join(secret_parts)
        trade_secrets += (f"; controls: {_item(keys['ipd_secret_controls'])}." if "ipd_secret_controls" in keys
                          else ". Protection controls (confidentiality, access restriction) not evidenced.")
    else:
        trade_secrets = f"{NOT_PROVIDED}: trade secrets and their protection controls not described."

    software_keys = ("eo_contractors", "ipo_copyleft", "ipo_open_source_general", "ipo_oss_compliance",
                     "tpd_licensed_core", "ipd_proprietary_data", "ai_scraped_data", "ai_customer_data")
    software_parts = [_item(keys[k]) for k in software_keys if k in keys]
    software_data = (_join(software_parts) + "." if software_parts
                     else f"{NOT_PROVIDED}: software authorship, open-source use and data rights not described.")

    ownership_keys = ("ipo_assignment", "ipo_company_owned", "ipo_ownership_unclear", "ipo_founder_personal",
                      "ipo_university", "ipo_prior_employer", "ipo_gov_funding", "ipo_joint_dev", "eo_contractors")
    ownership_parts = [_item(keys[k]) for k in ownership_keys if k in keys]
    ownership = _join(ownership_parts) if ownership_parts else ""
    if "ipo_assignment" not in keys:
        ownership = (ownership + ". " if ownership else "") + "Invention-assignment coverage NOT PROVIDED."
    elif ownership and not ownership.endswith("."):
        ownership += "."

    ipd = in_category(findings, C.IP_DEFENSIBILITY)
    positives = sorted((f for f in ipd if f.polarity == Polarity.POSITIVE and not f.planned), key=lambda f: -f.weight)
    negatives = [f for f in ipd if f.polarity == Polarity.NEGATIVE and not f.planned]
    defense_parts = []
    if positives:
        defense_parts.append("Moat evidence: " + _join(_item(f) for f in positives[:3]))
    if negatives:
        defense_parts.append("Weakened by: " + _join(_item(f) for f in negatives[:2]))
    if not defense_parts:
        defense_parts.append(f"{NOT_PROVIDED}: no defensibility evidence beyond product claims")
    if patent_parts:
        defense_parts.append("Patents are not treated as proof of defensibility without claim-to-product mapping")
    if "ex_audit" not in keys:
        defense_parts.append("No freedom-to-operate evidence")
    defensibility = ". ".join(defense_parts) + "."

    core_items = [f.statement for f in positives if f.topic in (Topic.PATENTS, Topic.TRADE_SECRETS, Topic.AI_DATA,
                                                                  Topic.SOFTWARE_IP)]
    core_ip = (_join(core_items[:4]) + "." if core_items
               else f"{NOT_PROVIDED}: core proprietary assets not identified in the deck.")

    if dependencies:
        ranked = sorted(dependencies, key=lambda d: ["CRITICAL", "HIGH", "MEDIUM", "LOW"].index(d.criticality.value))
        third_party = _join(f"{d.name} ({d.dependency_type}, {d.criticality.value.lower()})" for d in ranked[:5]) + "."
    else:
        third_party = f"{NOT_PROVIDED}: no third-party platforms, licenses or suppliers named."

    relevant = [f for f in findings if f.category in (C.IP_OWNERSHIP, C.IP_DEFENSIBILITY, C.THIRD_PARTY_DEPENDENCY)
                or {C.IP_OWNERSHIP, C.IP_DEFENSIBILITY} & set(f.secondary_categories)]
    gaps = gaps_for(C.IP_OWNERSHIP, findings) + gaps_for(C.IP_DEFENSIBILITY, findings) + \
        gaps_for(C.THIRD_PARTY_DEPENDENCY, findings)
    return IntellectualPropertyAssessment(
        summary=compose_summary(relevant, gaps, pos_limit=2, neg_limit=3, gap_limit=2),
        finding_ids=[f.id for f in relevant], gaps=gaps, core_ip=core_ip, ownership=ownership,
        defensibility=defensibility, patents=patents, trade_secrets=trade_secrets, software_data=software_data,
        third_party=third_party)


def assess_ai(findings: list[Finding], dependencies: list[ThirdPartyDependency], deck: DeckDocument) -> AIDataAssessment:
    if not is_ai_company(deck):
        return AIDataAssessment(applicable=False, summary="Not applicable: the deck does not describe an AI/ML product.",
                                proprietary_assessment="Not applicable.")
    keys = _by_key(findings)
    relevant = [f for f in findings if f.topic == Topic.AI_DATA or f.signal_key in
                {"tpd_foundation_model", "sc_inference_cost", "tpd_multi_vendor"}]
    third_party_model = "tpd_foundation_model" in keys or any(
        d.dependency_type == "Foundation-model API" for d in dependencies)
    proprietary = [keys[k] for k in ("ipd_proprietary_data", "ai_fine_tuned") if k in keys]

    if third_party_model and not proprietary:
        assessment = ("INFERENCE: the claimed AI advantage appears built primarily on third-party models; no "
                      "proprietary training data or model ownership is evidenced.")
    elif third_party_model:
        assessment = ("Mixed: proprietary data or models referenced alongside third-party model dependency; "
                      "verify data rights and model portability.")
    elif proprietary:
        assessment = ("Proprietary data or model assets referenced; training-data rights, model ownership and "
                      "reproducibility not evidenced.")
    else:
        assessment = f"{NOT_PROVIDED}: data rights, model ownership and model dependencies not described."

    expectations = [
        ("Training-data ownership / licensing", {"ipd_proprietary_data", "ai_scraped_data"}),
        ("Customer data rights", {"ai_customer_data"}),
        ("Model ownership and portability", {"ai_fine_tuned", "tpd_multi_vendor", "tpd_foundation_model"}),
        ("Inference and training cost", {"sc_inference_cost"}),
    ]
    gaps = [label for label, ks in expectations if not ks & set(keys)]
    return AIDataAssessment(applicable=True, summary=compose_summary(relevant, gaps, gap_limit=3),
                            finding_ids=[f.id for f in relevant], gaps=gaps, proprietary_assessment=assessment)
