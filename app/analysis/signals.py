"""Deterministic evidence detection: a catalog of diligence signals with slide references.

Each `SignalRule` describes one thing a technical or IP diligence reviewer looks
for (a SOC 2 attestation, contractor-built code, copyleft licenses...). Rules
are matched sentence by sentence, with two safeguards:

* negation — "no copyleft components" does not trigger copyleft exposure;
* plans — a *positive* claim in roadmap language ("SOC 2 planned for 2027") is
  recorded as planned, earns no score credit, and is reported as not completed.

Detection only records what the deck says. Interpretation (severity, scoring,
escalation) happens downstream in `red_flags` and `risk_scoring`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache

from app.ingestion.document import DeckDocument
from app.models.schemas import (
    Classification,
    CompanySnapshot,
    Finding,
    Polarity,
    ScoreCategory as C,
    Severity,
    ThirdPartyDependency,
    Topic as T,
)
from app.utils.context import is_future_context, is_negated
from app.utils.text import shorten, split_units

POS, NEG, NEU = Polarity.POSITIVE, Polarity.NEGATIVE, Polarity.NEUTRAL

AI_CONTEXT = re.compile(
    r"\b(?:ai|artificial intelligence|machine learning|ml|llms?|generative|gen ?ai|copilots?|agentic|"
    r"agents?|neural|deep learning|computer vision|nlp|foundation models?|large language models?)\b",
    re.IGNORECASE,
)
REGULATED_OR_ENTERPRISE = re.compile(
    r"\b(?:enterprise|fortune \d+|hospitals?|health systems?|patients?|phi|hipaa|banks?|financial "
    r"institutions?|insur(?:ance|ers?)|carriers?|government|federal|public sector|utilit(?:y|ies)|"
    r"payments?|defen[cs]e|dod|pharma|clinical)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SignalRule:
    key: str
    category: C
    topic: T
    polarity: Polarity
    weight: float
    label: str
    patterns: tuple[str, ...]
    flag: str | None = None
    also: tuple[C, ...] = ()
    ignore_future: bool = False
    exclude: tuple[str, ...] = ()
    scope: str = "text"
    needs_ai_context: bool = False


def _r(key, category, topic, polarity, weight, label, patterns, **kw) -> SignalRule:
    return SignalRule(key, category, topic, polarity, weight, label, tuple(patterns), **kw)


RULES: tuple[SignalRule, ...] = (
    # --- Technical architecture -------------------------------------------------
    _r("ta_architecture_slide", C.TECHNICAL_ARCHITECTURE, T.ARCHITECTURE, POS, 0.5,
       "dedicated architecture / technology slide",
       [r"\barchitecture\b", r"\btech(?:nology)? stack\b", r"\bplatform overview\b", r"\bhow it works\b",
        r"\bsystem design\b", r"\binfrastructure\b"], scope="title"),
    _r("ta_microservices", C.TECHNICAL_ARCHITECTURE, T.ARCHITECTURE, POS, 1.0,
       "modular / service-oriented architecture",
       [r"micro-?services", r"service-oriented architecture", r"modular (?:architecture|platform|design|services)",
        r"multi-tenant (?:architecture|platform|saas)"]),
    _r("ta_api", C.TECHNICAL_ARCHITECTURE, T.ARCHITECTURE, POS, 0.5, "documented API / SDK layer",
       [r"\b(?:rest|restful|graphql|grpc|open|public|documented)\s+(?:and\s+\w+\s+)?apis?\b", r"\bapi-first\b",
        r"\bapis? (?:and|&) sdks?\b"]),
    _r("ta_cloud_native", C.TECHNICAL_ARCHITECTURE, T.ARCHITECTURE, POS, 0.75,
       "cloud-native, containerized deployment",
       [r"\bkubernetes\b", r"\bk8s\b", r"containeri[sz]ed", r"\bdocker\b", r"\bserverless\b", r"cloud-native"]),
    _r("ta_iac", C.TECHNICAL_ARCHITECTURE, T.ARCHITECTURE, POS, 0.75, "infrastructure as code",
       [r"\bterraform\b", r"infrastructure[- ]as[- ]code", r"\bpulumi\b", r"\bcloudformation\b"],
       also=(C.TECHNICAL_DOCUMENTATION,)),
    _r("ta_multiregion", C.TECHNICAL_ARCHITECTURE, T.ARCHITECTURE, POS, 1.0,
       "multi-region / high-availability deployment",
       [r"multi-?region", r"active-active", r"high[- ]availability", r"automated failover",
        r"redundant (?:data ?centers|regions|infrastructure)"], also=(C.SCALABILITY,)),
    _r("ta_observability", C.TECHNICAL_ARCHITECTURE, T.ARCHITECTURE, POS, 0.5, "observability / monitoring",
       [r"\bobservability\b", r"\bdatadog\b", r"\bprometheus\b", r"\bgrafana\b", r"\bnew relic\b",
        r"\bopentelemetry\b", r"24/7 monitoring"]),
    _r("ta_monolith", C.TECHNICAL_ARCHITECTURE, T.ARCHITECTURE, NEG, 0.75, "monolithic application",
       [r"\bmonolith(?:ic)?\b"]),
    _r("ta_legacy", C.TECHNICAL_ARCHITECTURE, T.ARCHITECTURE, NEG, 1.0, "legacy components in production stack",
       [r"\blegacy (?:code|system|platform|stack|codebase|components?|software)\b",
        r"\b(?:cobol|vb6|visual basic 6|python 2(?:\.7)?|php ?5|angularjs)\b"], flag="LEGACY_ARCHITECTURE"),
    _r("ta_single_server", C.TECHNICAL_ARCHITECTURE, T.ARCHITECTURE, NEG, 1.25,
       "single server / single point of failure",
       [r"single (?:\w+ )?(?:server|instance|vm|virtual machine|data ?center|database)\b",
        r"single point of failure", r"on-?prem(?:ise)? server in (?:our|the) office"],
       flag="SINGLE_POINT_OF_FAILURE", also=(C.SCALABILITY,)),
    _r("ta_manual_integrations", C.TECHNICAL_ARCHITECTURE, T.ARCHITECTURE, NEG, 0.75,
       "manual or custom per-customer integrations",
       [r"custom integrations? (?:for|per) (?:each|every) (?:customer|client|pilot)",
        r"manual (?:data )?(?:uploads?|integrations?|imports?)", r"screen[- ]scrap\w*",
        r"(?:csv|excel) (?:uploads?|imports?)"], flag="FRAGILE_INTEGRATIONS", also=(C.SCALABILITY,)),
    _r("ta_customer_forks", C.TECHNICAL_ARCHITECTURE, T.ARCHITECTURE, NEG, 1.0,
       "customer-specific code branches or builds",
       [r"customer-specific (?:versions?|builds?|forks?|code|branches|deployments?)",
        r"(?:separate|dedicated|own) (?:code )?(?:branch|fork|version|build|codebase) (?:for|per) (?:each|every) "
        r"(?:customer|client|pilot)",
        r"bespoke (?:builds?|versions?|deployments?) (?:for|per) (?:each )?(?:customer|client)"],
       flag="CUSTOMER_CODE_FORKS", also=(C.TECHNICAL_DOCUMENTATION, C.EXIT_READINESS)),
    # --- Development maturity ---------------------------------------------------
    _r("dm_production", C.DEVELOPMENT_MATURITY, T.MATURITY, POS, 1.0, "product in production with customers",
       [r"\bin production\b", r"generally available", r"\bGA since\b", r"\blive (?:with|at|across) \d",
        r"deployed (?:at|to|with|across) \d", r"production (?:deployments?|customers|environments?)"]),
    _r("dm_scaled", C.DEVELOPMENT_MATURITY, T.MATURITY, POS, 1.25, "operating at measurable scale",
       [r"\b\d[\d,.]*\s*(?:k|m|mm|b|bn|million|billion|thousand)?\+?\s*(?:api calls|transactions|requests|"
        r"events|telemetry events|devices|daily active users|monthly active users|endpoints|claims|records)\s*"
        r"(?:per|/|a|each)\s*(?:day|month|second|sec|year)",
        r"process(?:es|ed)? (?:over |more than )?\$?\d[\d,.]*\s*(?:m|b|bn|million|billion)\b"],
       also=(C.SCALABILITY,)),
    _r("dm_uptime", C.DEVELOPMENT_MATURITY, T.MATURITY, POS, 1.0, "measured uptime / SLA",
       [r"\b99\.\d+\s*%\s*(?:uptime|availability|sla)", r"\buptime (?:sla )?of 99", r"\b99\.\d+\s*%.{0,20}\bsla\b"],
       also=(C.TECHNICAL_ARCHITECTURE,)),
    _r("dm_cicd", C.DEVELOPMENT_MATURITY, T.MATURITY, POS, 1.0, "CI/CD pipeline",
       [r"\bci/cd\b", r"continuous (?:integration|deployment|delivery)", r"\bgithub actions\b", r"\bgitlab ci\b",
        r"\bjenkins\b", r"\bcircleci\b"], also=(C.TECHNICAL_DOCUMENTATION,)),
    _r("dm_testing", C.DEVELOPMENT_MATURITY, T.MATURITY, POS, 0.75, "automated testing",
       [r"automated (?:tests?|testing|qa|test coverage)", r"\bunit tests?\b", r"\bintegration tests?\b",
        r"\b\d{1,3}\s*%\s*(?:automated |code |unit |test )*(?:test )?coverage"]),
    _r("dm_release", C.DEVELOPMENT_MATURITY, T.MATURITY, POS, 0.5, "regular release cadence",
       [r"(?:weekly|daily|bi-?weekly) (?:releases?|deploy(?:ment)?s)", r"release (?:cadence|management|train)",
        r"\d+\s*(?:releases|deploys|deployments) (?:per|a) (?:week|month|day)"]),
    _r("dm_dr", C.DEVELOPMENT_MATURITY, T.MATURITY, POS, 0.75, "disaster recovery / backups",
       [r"disaster recovery", r"\bdr (?:plan|tests?|drills?)\b", r"business continuity", r"\brpo\b", r"\brto\b",
        r"backups? (?:tested|daily|hourly|encrypted|restored)"], also=(C.CYBERSECURITY_COMPLIANCE,)),
    _r("dm_incident", C.DEVELOPMENT_MATURITY, T.MATURITY, POS, 0.5, "incident management process",
       [r"incident (?:response|management)", r"on-?call rotation", r"post-?mortems?", r"\bpagerduty\b"],
       also=(C.CYBERSECURITY_COMPLIANCE,)),
    _r("dm_prototype", C.DEVELOPMENT_MATURITY, T.MATURITY, NEG, 1.25, "prototype / MVP stage",
       [r"\bprototype\b", r"\bproof[- ]of[- ]concept\b", r"\bpoc\b", r"\bmvp\b", r"minimum viable product",
        r"\balpha (?:version|release|product)\b"], flag="PROTOTYPE_STAGE", ignore_future=True),
    _r("dm_beta_pilot", C.DEVELOPMENT_MATURITY, T.MATURITY, NEG, 0.5, "beta / pilot-stage deployments",
       [r"\bbeta\b", r"\bpilots?\b", r"\bdesign partners?\b"], ignore_future=True),
    _r("dm_no_testing", C.DEVELOPMENT_MATURITY, T.MATURITY, NEG, 1.0, "limited or manual testing",
       [r"\bno (?:automated )?(?:tests|testing|qa)\b", r"\bmanual (?:testing|qa)\b",
        r"\blimited (?:tests?|testing|qa)\b"], flag="NO_QA_PROCESS"),
    # --- Scalability -------------------------------------------------------------
    _r("sc_autoscale", C.SCALABILITY, T.SCALABILITY, POS, 1.0, "horizontal scaling / autoscaling",
       [r"auto-?scal\w+", r"horizontally scal\w+", r"scales? horizontally", r"elastic (?:scaling|infrastructure)"]),
    _r("sc_load_tested", C.SCALABILITY, T.SCALABILITY, POS, 0.75, "load-tested headroom",
       [r"load[- ]test\w*", r"stress[- ]test\w*", r"tested (?:to|at) \d[\d,.]*\s*(?:x|k|m|million|concurrent)",
        r"benchmark\w* (?:at|to) \d"]),
    _r("sc_bottleneck", C.SCALABILITY, T.SCALABILITY, NEG, 1.0, "disclosed scaling bottleneck",
       [r"\bbottlenecks?\b", r"capacity (?:constraints?|limits?)", r"(?:does not|doesn't|cannot|can't) (?:yet )?scale",
        r"scaling (?:issues|challenges|problems)", r"re-?platform\w*"], flag="SCALABILITY_BOTTLENECK",
       ignore_future=True),
    _r("sc_manual_services", C.SCALABILITY, T.ECONOMICS, NEG, 1.0, "services-heavy implementation",
       [r"\b(?:\d+|several|multi)[- ](?:week|month)s? (?:implementation|onboarding|deployment|integration)",
        r"professional services", r"white[- ]glove", r"manual onboarding", r"implementation (?:fees|team)",
        r"human[- ]in[- ]the[- ]loop (?:review|labell?ing)"], flag="SERVICES_HEAVY_DELIVERY"),
    _r("sc_inference_cost", C.SCALABILITY, T.ECONOMICS, NEG, 0.75, "variable inference / API cost",
       [r"inference costs?", r"token costs?", r"\bapi costs?\b", r"per-(?:token|call|request) (?:pricing|costs?)",
        r"\bgpu costs?\b"], flag="INFERENCE_COST_EXPOSURE", also=(C.THIRD_PARTY_DEPENDENCY,)),
    _r("sc_hardware_cost", C.SCALABILITY, T.ECONOMICS, NEG, 0.5, "hardware / bill-of-materials cost exposure",
       [r"bill of materials", r"\bbom\b", r"hardware costs?", r"unit costs? of (?:the )?(?:device|hardware)"]),
    # --- Engineering organization ------------------------------------------------
    _r("eo_cto", C.ENGINEERING_ORGANIZATION, T.ENGINEERING_ORG, POS, 1.0, "dedicated CTO / engineering leader",
       [r"\bcto\b", r"chief technology officer", r"\bvp,? (?:of )?engineering\b", r"head of engineering"],
       exclude=(r"(?:hiring|recruit\w*|seeking|search(?:ing)? for|looking for|need|fractional|interim|"
                r"part-time|advis\w+|to hire|will hire)\W+(?:\w+\W+){0,3}(?:cto|vp|head of eng|chief tech)",)),
    _r("eo_technical_founder", C.ENGINEERING_ORGANIZATION, T.ENGINEERING_ORG, POS, 0.5, "technical co-founder",
       [r"technical co-?founder", r"founding engineer"]),
    _r("eo_cto_missing", C.ENGINEERING_ORGANIZATION, T.ENGINEERING_ORG, NEG, 1.25,
       "no full-time technical leader in place",
       [r"(?:hiring|recruiting|seeking|search(?:ing)? for|looking for|need|to hire|will hire) (?:a |an |our )?"
        r"(?:first |full-time )?(?:cto|vp,? (?:of )?engineering|head of engineering|chief technology officer)"],
       flag="NO_TECHNICAL_LEADER", ignore_future=True),
    _r("eo_fractional_cto", C.ENGINEERING_ORGANIZATION, T.ENGINEERING_ORG, NEG, 1.0,
       "fractional or interim technical leadership",
       [r"(?:fractional|interim|part-time|advisory|contract) (?:cto|vp,? engineering|head of engineering)"],
       flag="FRACTIONAL_TECH_LEADERSHIP"),
    _r("eo_inhouse", C.ENGINEERING_ORGANIZATION, T.ENGINEERING_ORG, POS, 0.75, "in-house engineering team",
       [r"in-house (?:engineering|development|engineers|team)", r"all (?:code|development|engineering) "
        r"(?:is )?(?:done |built |written )?in-house", r"employee-built"], also=(C.IP_OWNERSHIP,)),
    _r("eo_founder_dependency", C.ENGINEERING_ORGANIZATION, T.ENGINEERING_ORG, NEG, 1.5,
       "core technology dependent on one founder / engineer",
       [r"(?:sole|only|single|lone) (?:engineer|developer|programmer|technical (?:founder|co-?founder))",
        r"(?:founder|ceo|cto|co-?founder),? (?:who )?(?:single-handedly |personally )?(?:wrote|built|coded|developed)"
        r" (?:the |all |most )?(?:of )?(?:(?:entire|core|original) )?(?:[\w-]+ )?(?:platform|code(?:base)?|product|"
        r"software|algorithms?|engine|mvp)",
        r"one-person (?:engineering|tech|development) team",
        r"all (?:code|development) (?:is )?(?:written|done) by (?:the |our )?(?:founder|cto)"],
       flag="KEY_PERSON_DEPENDENCY", also=(C.TECHNICAL_DOCUMENTATION, C.EXIT_READINESS)),
    _r("eo_contractors", C.ENGINEERING_ORGANIZATION, T.ENGINEERING_ORG, NEG, 1.25,
       "development by contractors / outsourced agency",
       [r"(?:outsourc\w+|offshore|nearshore|freelance|contract(?:ed)?) (?:development|developers|dev (?:team|shop|agency)"
        r"|engineering|engineers|software team)",
        r"(?:dev(?:elopment)? (?:shop|agency)|software agency|freelancers?|contractors?) (?:built|developed|wrote|"
        r"delivered|maintains?|maintained)",
        r"built by (?:an? )?(?:\w+ )?(?:agency|contractors?|freelancers?|outsourc\w+|offshore)",
        r"(?:development|engineering) (?:is |was )?outsourced", r"\b(?:upwork|toptal|fiverr)\b",
        r"freelance (?:developers|engineers)"],
       flag="CONTRACTOR_DEVELOPED_CODE", also=(C.IP_OWNERSHIP,)),
    # --- Technical documentation ---------------------------------------------------
    _r("td_docs", C.TECHNICAL_DOCUMENTATION, T.DOCUMENTATION, POS, 1.0, "maintained technical documentation",
       [r"(?:architecture|technical|system|api|developer) documentation", r"documented (?:apis?|architecture|"
        r"codebase|processes|runbooks)", r"\brunbooks?\b", r"architecture decision records?", r"\badrs?\b",
        r"\bdesign docs\b"]),
    _r("td_code_review", C.TECHNICAL_DOCUMENTATION, T.DOCUMENTATION, POS, 0.5, "code review / static analysis",
       [r"code reviews?", r"pull[- ]request reviews?", r"static analysis", r"\bsonarqube\b"]),
    _r("td_undocumented", C.TECHNICAL_DOCUMENTATION, T.DOCUMENTATION, NEG, 1.25, "limited technical documentation",
       [r"(?:no|little|limited|minimal|sparse|poor) (?:formal )?(?:technical )?documentation",
        r"lack of (?:formal )?documentation", r"\bundocumented\b", r"tribal knowledge",
        r"knowledge (?:resides|lives|sits) (?:with|in) (?:the )?(?:founder|cto|one)"], flag="UNDOCUMENTED_CODEBASE"),
    _r("td_tech_debt", C.TECHNICAL_DOCUMENTATION, T.DOCUMENTATION, NEG, 0.75, "technical debt / planned rebuild",
       [r"technical debt", r"tech debt", r"re-?architect(?:ure|ing)?", r"\brebuild (?:of )?(?:the |our )?"
        r"(?:platform|codebase|product|system|stack)", r"rewrite (?:of )?(?:the |our )?(?:platform|codebase)"],
       flag="TECH_DEBT", ignore_future=True, also=(C.EXIT_READINESS,)),
    # --- Cybersecurity / compliance -------------------------------------------------
    _r("cy_soc2", C.CYBERSECURITY_COMPLIANCE, T.COMPLIANCE, POS, 1.5, "SOC 2 attestation",
       [r"\bsoc ?2\b"]),
    _r("cy_iso27001", C.CYBERSECURITY_COMPLIANCE, T.COMPLIANCE, POS, 1.25, "ISO 27001 certification",
       [r"\biso[/ ]?(?:iec )?27001\b"]),
    _r("cy_hipaa", C.CYBERSECURITY_COMPLIANCE, T.COMPLIANCE, POS, 0.75, "HIPAA compliance program",
       [r"hipaa[- ]compliant", r"hipaa compliance", r"business associate agreements?", r"\bbaas?\b"]),
    _r("cy_gdpr", C.CYBERSECURITY_COMPLIANCE, T.COMPLIANCE, POS, 0.5, "GDPR compliance",
       [r"gdpr[- ](?:compliant|compliance|ready)", r"data processing agreements?"]),
    _r("cy_pci", C.CYBERSECURITY_COMPLIANCE, T.COMPLIANCE, POS, 0.75, "PCI DSS compliance",
       [r"\bpci[- ]dss\b", r"\bpci (?:compliant|compliance|level \d)"]),
    _r("cy_fedramp", C.CYBERSECURITY_COMPLIANCE, T.COMPLIANCE, POS, 1.0, "FedRAMP / government authorization",
       [r"\bfedramp\b", r"\bstateramp\b", r"\bcmmc\b"]),
    _r("cy_fda_cyber", C.CYBERSECURITY_COMPLIANCE, T.COMPLIANCE, POS, 0.5, "medical-device cybersecurity controls",
       [r"fda (?:premarket )?cybersecurity", r"\bsbom\b", r"software bill of materials", r"iec 62304", r"iec 81001"]),
    _r("cy_pentest", C.CYBERSECURITY_COMPLIANCE, T.SECURITY, POS, 0.75, "penetration testing",
       [r"penetration test\w*", r"\bpen[- ]?test\w*", r"bug bounty", r"third-party security (?:audit|assessment)"]),
    _r("cy_encryption", C.CYBERSECURITY_COMPLIANCE, T.SECURITY, POS, 0.5, "encryption at rest / in transit",
       [r"encrypt(?:ed|ion)", r"\baes-?256\b", r"\btls ?1\.[23]\b"]),
    _r("cy_access", C.CYBERSECURITY_COMPLIANCE, T.SECURITY, POS, 0.5, "SSO / MFA / role-based access",
       [r"\bsso\b", r"single sign-on", r"\bsaml\b", r"\bmfa\b", r"multi-factor", r"role-based access", r"\brbac\b",
        r"zero[- ]trust", r"least privilege"]),
    _r("cy_no_security", C.CYBERSECURITY_COMPLIANCE, T.SECURITY, NEG, 1.25, "no formal security program",
       [r"no (?:formal )?(?:security|soc ?2|infosec|cybersecurity) (?:program|certification|audit|team|policies)",
        r"security (?:is )?(?:not yet|has not been) (?:audited|addressed|prioriti[sz]ed)", r"not (?:yet )?soc ?2"],
       flag="NO_SECURITY_PROGRAM", ignore_future=True),
    _r("cy_incident", C.CYBERSECURITY_COMPLIANCE, T.SECURITY, NEG, 1.5, "prior security incident",
       [r"(?:we|company|our platform) (?:experienced|suffered|had) (?:a )?(?:data )?(?:breach|security incident|"
        r"ransomware)"], flag="SECURITY_INCIDENT"),
    # --- IP ownership -----------------------------------------------------------------
    _r("ipo_assignment", C.IP_OWNERSHIP, T.OWNERSHIP, POS, 1.5, "invention-assignment agreements in place",
       [r"(?:invention|ip|intellectual property) assignment", r"\bpiias?\b", r"\bciiaas?\b",
        r"proprietary information and inventions", r"(?:employees|staff|engineers|contributors)(?: and contractors)?"
        r" (?:have )?(?:signed|executed) (?:\w+ )?(?:ip|invention|assignment)"],
       exclude=(r"being (?:finali[sz]ed|negotiated|resolved)|\bpending\b|outstanding|incomplete|in progress|"
                r"not yet|unclear|uncertain|to be (?:signed|executed|finali[sz]ed)",)),
    _r("ipo_company_owned", C.IP_OWNERSHIP, T.OWNERSHIP, POS, 1.0, "core IP owned by / assigned to the company",
       [r"(?:patents?|ip|intellectual property|code(?:base)?|source code|technology),? (?:are |is )?(?:all )?"
        r"(?:wholly |fully |100% )?(?:owned by|assigned to) (?:the company|us|[A-Z][\w&]*)",
        r"(?:wholly|fully|100%) (?:company-)?owned (?:ip|intellectual property|code|technology)",
        r"clean (?:chain of title|ip)"]),
    _r("ipo_oss_compliance", C.IP_OWNERSHIP, T.SOFTWARE_IP, POS, 0.75, "open-source license scanning / policy",
       [r"(?:open[- ]source|oss|license) (?:license )?(?:compliance|scanning|scans?|audit|review|policy)",
        r"\bfossa\b", r"black ?duck", r"\bsnyk\b", r"software composition analysis"]),
    _r("ipo_open_source_general", C.IP_OWNERSHIP, T.SOFTWARE_IP, NEG, 0.5, "open-source components in product",
       [r"open[- ]source (?:components?|libraries|software|stack|models?|frameworks?|code|packages)",
        r"built on (?:top of )?open[- ]source"], flag="OPEN_SOURCE_UNREVIEWED"),
    _r("ipo_copyleft", C.IP_OWNERSHIP, T.SOFTWARE_IP, NEG, 1.5, "copyleft (GPL-family) components",
       [r"\ba?gpl(?:v?[23])?\b", r"\blgpl\b", r"\bcopyleft\b", r"\bsspl\b", r"\baffero\b"],
       flag="COPYLEFT_OPEN_SOURCE", also=(C.IP_DEFENSIBILITY, C.EXIT_READINESS)),
    _r("ipo_university", C.IP_OWNERSHIP, T.OWNERSHIP, NEG, 1.25, "technology originated at / licensed from a university",
       [r"licensed from (?:the )?(?:[\w ]{0,40})?(?:university|institute of technology|college)",
        r"university (?:license|licensed|spin-?out|spinout|technology transfer|ip)",
        r"(?:exclusive|non-exclusive) license (?:from|with) (?:the )?[\w ]{0,40}(?:university|institute|college|lab)",
        r"tech(?:nology)? transfer office", r"developed (?:at|during) (?:(?:my|his|her|their|the|our) )?"
        r"(?:founder'?s? |ceo'?s? |cto'?s? )?(?:phd|doctoral|postdoc\w*|graduate|research)(?: \w+)? (?:at|in)",
        r"spun out of", r"\bfrom (?:the )?[\w ]{0,30}university\b"],
       flag="UNIVERSITY_IP", also=(C.THIRD_PARTY_DEPENDENCY,), ignore_future=True),
    _r("ipo_prior_employer", C.IP_OWNERSHIP, T.OWNERSHIP, NEG, 1.5, "technology linked to a prior employer",
       [r"(?:developed|built|created|invented|conceived) (?:while|during (?:(?:his|her|their|my) )?(?:time|tenure|"
        r"employment)) (?:at|with)", r"(?:prior|former|previous) employer", r"while (?:still )?(?:employed|working) at"],
       flag="PRIOR_EMPLOYER_CLAIM"),
    _r("ipo_gov_funding", C.IP_OWNERSHIP, T.OWNERSHIP, NEG, 0.5, "government-funded development",
       [r"\bsbir\b", r"\bsttr\b", r"\bdarpa\b", r"\bnih (?:grant|funded|funding)", r"\bnsf (?:grant|funded|funding)",
        r"\bdoe (?:grant|funded|funding)", r"government[- ]funded", r"federal (?:grants?|funding)", r"bayh-dole",
        r"\bbarda\b", r"\bafwerx\b"], flag="GOVERNMENT_FUNDED_IP"),
    _r("ipo_joint_dev", C.IP_OWNERSHIP, T.OWNERSHIP, NEG, 1.0, "joint development / shared IP ownership",
       [r"joint(?:ly)?[- ]develop\w*", r"\bco-?develop\w*", r"joint (?:ip|ownership)", r"jointly owned",
        r"customer (?:owns|retains) (?:the )?(?:ip|rights|code|models?)", r"(?:ip|rights) (?:shared|co-owned) with"],
       flag="JOINT_DEVELOPMENT_IP"),
    _r("ipo_founder_personal", C.IP_OWNERSHIP, T.OWNERSHIP, NEG, 1.25, "core IP held personally by a founder",
       [r"(?:patents?|ip|code|technology) (?:is |are )?(?:held|owned) (?:personally )?by (?:the |our )?"
        r"(?:founders?|ceo|cto|inventors?)", r"(?:founder|ceo|cto|inventor)s?'? (?:personally )?(?:owns?|holds?) "
        r"(?:the )?(?:patents?|ip|code)", r"(?:will be|to be) assigned to the company"],
       flag="FOUNDER_HELD_IP", ignore_future=True),
    _r("ipo_ownership_unclear", C.IP_OWNERSHIP, T.OWNERSHIP, NEG, 1.25, "IP ownership / assignment unresolved",
       [r"(?:ip|ownership|chain of title|assignments?|licen[cs]e)(?: \w+){0,4} (?:is |are )?(?:still |currently )?"
        r"(?:unclear|uncertain|"
        r"being (?:resolved|clarified|finali[sz]ed|negotiated)|to be (?:confirmed|determined|clarified|finali[sz]ed)|"
        r"pending|outstanding|incomplete|in negotiation)"],
       flag="IP_OWNERSHIP_UNCLEAR", ignore_future=True, also=(C.EXIT_READINESS,)),
    # --- IP defensibility -----------------------------------------------------------
    _r("ipd_issued_patents", C.IP_DEFENSIBILITY, T.PATENTS, POS, 1.5, "issued patents",
       [r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve)\s+(?:issued|granted)\s+"
        r"(?:u\.?s\.?\s+|us\s+|eu\s+|european\s+)?patents?", r"(?:issued|granted) (?:u\.?s\.?\s+)?patents?",
        r"\bu\.?s\.? patent (?:no\.?|number) ?[\d,]+", r"\bpatent (?:no\.?|number) ?\d"]),
    _r("ipd_pending_patents", C.IP_DEFENSIBILITY, T.PATENTS, POS, 0.5, "pending patent applications",
       [r"patents? pending", r"pending (?:patent )?applications?", r"patent applications? (?:filed|pending)",
        r"(?:provisional|non-provisional|pct|utility) (?:patent )?(?:applications?|filings?)",
        r"filed (?:\d+ )?(?:patent|provisional)"], ignore_future=True),
    _r("ipd_patent_plan", C.IP_DEFENSIBILITY, T.PATENTS, NEG, 0.75, "patent filings only planned",
       [r"(?:plan|intend|expect|looking) to (?:file|pursue|seek) (?:\w+ )?patents?",
        r"patent strategy (?:is )?(?:planned|under development|to be developed|tbd)",
        r"(?:will|to) file (?:\w+ )?patents?", r"\bno (?:issued |filed )?patents?\b", r"patents? not yet filed"],
       flag="NO_PATENT_PROTECTION", ignore_future=True),
    _r("ipd_trade_secrets", C.IP_DEFENSIBILITY, T.TRADE_SECRETS, POS, 0.75, "trade secrets / proprietary know-how",
       [r"trade secrets?", r"proprietary (?:know-how|process(?:es)?|methods?|formulations?|manufacturing)"]),
    _r("ipd_secret_controls", C.IP_DEFENSIBILITY, T.TRADE_SECRETS, POS, 0.5, "confidentiality / access controls on IP",
       [r"confidentiality agreements?", r"access controls? (?:on|to|for) (?:source|code|models|data)",
        r"need-to-know", r"(?:source )?code escrow"], also=(C.IP_OWNERSHIP,)),
    _r("ipd_proprietary_data", C.IP_DEFENSIBILITY, T.AI_DATA, POS, 1.0, "proprietary dataset",
       [r"proprietary (?:data(?:sets?)?|training data|corpus|database)", r"exclusive (?:data|datasets?)\b",
        r"data (?:moat|flywheel|network effects?)",
        r"\b\d[\d,.]*\s*(?:k|m|million|b|billion)?\+?\s*(?:labell?ed|annotated|proprietary) (?:\w+ )?"
        r"(?:images|records|samples|data points|examples|events)"]),
    _r("ipd_proprietary_algo", C.IP_DEFENSIBILITY, T.SOFTWARE_IP, POS, 0.5, "proprietary algorithms / models",
       [r"proprietary (?:algorithms?|models?|engine|software|technology|forecasting|dispatch)",
        r"patented (?:technology|process|methods?)"]),
    _r("ipd_network_effects", C.IP_DEFENSIBILITY, T.PATENTS, POS, 0.5, "workflow lock-in / network effects",
       [r"network effects?", r"switching costs?", r"embedded in (?:\w+ )?workflows?", r"system of record"]),
    _r("ipd_regulatory_moat", C.IP_DEFENSIBILITY, T.COMPLIANCE, POS, 0.75, "regulatory clearance / exclusivity",
       [r"fda (?:510\(k\)|clearance|cleared|approval|approved|de novo|breakthrough)", r"\bce mark(?:ed)?\b",
        r"orphan (?:drug )?designation", r"regulatory exclusivity"]),
    _r("ipd_commodity", C.IP_DEFENSIBILITY, T.SOFTWARE_IP, NEG, 1.0, "thin layer over off-the-shelf technology",
       [r"(?:wrapper|thin layer|prompt layer) (?:around|on top of|over)", r"off-the-shelf (?:models?|components?|"
        r"software)", r"\bwhite-?label(?:ed)?\b"], flag="COMMODITY_TECHNOLOGY"),
    _r("ai_fine_tuned", C.IP_DEFENSIBILITY, T.AI_DATA, POS, 0.5, "fine-tuned or in-house trained models",
       [r"fine-?tuned (?:\w+ )?models?", r"(?:custom|in-house) trained models?", r"trained (?:our own|in-house|"
        r"proprietary) models?", r"(?:our own|in-house|self-hosted) models?"], needs_ai_context=True),
    _r("ai_scraped_data", C.IP_OWNERSHIP, T.AI_DATA, NEG, 1.0, "training data sourced by scraping",
       [r"scraped (?:\w+ )?(?:data|content|claims|images|text)", r"web[- ]scrap\w+",
        r"publicly available (?:web )?data (?:to|for) train"], flag="TRAINING_DATA_RIGHTS",
       also=(C.IP_DEFENSIBILITY,)),
    _r("ai_customer_data", C.IP_OWNERSHIP, T.AI_DATA, NEG, 0.5, "customer data used to train models",
       [r"(?:train\w*|fine-?tun\w*|improv\w*) (?:\w+ )?(?:on|with|using) (?:our )?customers?'? data",
        r"customer data (?:is used )?to (?:train|improve) (?:our )?models?"], flag="CUSTOMER_DATA_RIGHTS",
       needs_ai_context=True),
    # --- Third-party dependency -----------------------------------------------------
    _r("tpd_foundation_model", C.THIRD_PARTY_DEPENDENCY, T.DEPENDENCY, NEG, 1.25,
       "core AI built on third-party foundation-model APIs",
       [r"\b(?:built|runs?|powered|relies|based|depends?|running) (?:entirely |primarily |exclusively |directly )?"
        r"(?:on|by) (?:the )?(?:openai|gpt-?\d(?:\.\d)?\w*|chatgpt|anthropic|claude|gemini|google'?s? gemini|"
        r"llama|mistral|cohere)\b",
        r"\b(?:openai|anthropic|gemini) (?:gpt-?\d\w* )?apis?\b", r"\bgpt-?[345](?:\.\d)?(?:o|-turbo)? apis?\b"],
       flag="FOUNDATION_MODEL_DEPENDENCY", also=(C.IP_DEFENSIBILITY, C.SCALABILITY)),
    _r("tpd_single_cloud", C.THIRD_PARTY_DEPENDENCY, T.DEPENDENCY, NEG, 0.5, "exclusive reliance on one cloud",
       [r"(?:exclusively|only|entirely|solely) (?:on|hosted on|deployed on|built on) (?:aws|amazon web services|"
        r"azure|google cloud|gcp)"], flag="SINGLE_CLOUD_DEPENDENCY"),
    _r("tpd_sole_supplier", C.THIRD_PARTY_DEPENDENCY, T.DEPENDENCY, NEG, 1.0, "single-source supplier",
       [r"(?:single|sole|one) (?:supplier|source|vendor|contract manufacturer|fab|foundry)\b", r"sole-sourced?",
        r"single-sourced?"], flag="SUPPLIER_CONCENTRATION"),
    _r("tpd_licensed_core", C.THIRD_PARTY_DEPENDENCY, T.DEPENDENCY, NEG, 1.0, "core technology licensed from a third party",
       [r"(?:core|key|underlying|primary) (?:technology|engine|algorithm|ip|software) (?:is )?licensed (?:from|by)",
        r"\boem (?:license|agreement)\b"], flag="LICENSED_CORE_TECHNOLOGY", also=(C.IP_OWNERSHIP,)),
    _r("tpd_change_of_control", C.THIRD_PARTY_DEPENDENCY, T.DEPENDENCY, NEG, 1.0, "change-of-control / anti-assignment terms",
       [r"change[- ]of[- ]control", r"anti-assignment", r"non-transferable licen[sc]e", r"consent (?:to|for) assign\w*"],
       flag="CHANGE_OF_CONTROL_RESTRICTION", also=(C.EXIT_READINESS,)),
    _r("tpd_multi_vendor", C.THIRD_PARTY_DEPENDENCY, T.DEPENDENCY, POS, 0.75, "vendor-agnostic / multi-provider design",
       [r"model[- ]agnostic", r"vendor[- ]agnostic", r"cloud[- ]agnostic", r"multi-?cloud",
        r"multiple (?:model|llm|cloud) providers", r"aws and azure", r"azure and aws", r"aws and (?:gcp|google cloud)"]),
    # --- Exit readiness -----------------------------------------------------------------
    _r("ex_audit", C.EXIT_READINESS, T.EXIT, POS, 1.0, "independent code / IP audit or data room",
       [r"(?:ip|technical|code|security|source code) (?:and (?:code|ip) )?(?:audit|diligence|review) (?:completed|"
        r"performed|conducted)", r"(?:independent|third-party) (?:ip and )?(?:code|ip|technical) (?:and \w+ )?audit",
        r"(?:freedom[- ]to[- ]operate|fto) (?:analysis|opinion|search) (?:completed|conducted|obtained)",
        r"\bdata ?room\b"]),
)

RULE_INDEX: dict[str, SignalRule] = {rule.key: rule for rule in RULES}


@dataclass
class DetectedSignal:
    rule: SignalRule
    slides: list[int] = field(default_factory=list)
    excerpt: str = ""
    planned: bool = False
    label: str = ""
    weight: float | None = None
    polarity: Polarity | None = None
    flag: str | None = None

    @property
    def key(self) -> str:
        return self.rule.key

    @property
    def effective_label(self) -> str:
        return self.label or self.rule.label

    @property
    def effective_weight(self) -> float:
        return self.rule.weight if self.weight is None else self.weight

    @property
    def effective_polarity(self) -> Polarity:
        return self.polarity or self.rule.polarity

    @property
    def effective_flag(self) -> str | None:
        return self.flag if self.flag is not None else self.rule.flag


@lru_cache(maxsize=1)
def _compiled() -> dict[str, tuple[list[re.Pattern], list[re.Pattern]]]:
    return {
        rule.key: ([re.compile(p, re.IGNORECASE) for p in rule.patterns],
                   [re.compile(p, re.IGNORECASE) for p in rule.exclude])
        for rule in RULES
    }


def detect_signals(deck: DeckDocument, snapshot: CompanySnapshot, today: date | None = None) -> dict[str, DetectedSignal]:
    """Return one detected signal per rule key, with every supporting slide."""
    compiled = _compiled()
    ai_context = len(AI_CONTEXT.findall(deck.full_text)) >= 2
    found: dict[str, DetectedSignal] = {}

    def record(rule: SignalRule, slide: int, excerpt: str, planned: bool) -> None:
        current = found.get(rule.key)
        if current is None:
            found[rule.key] = DetectedSignal(rule, [slide], shorten(excerpt, 240), planned)
        elif current.planned and not planned:
            current.planned, current.slides, current.excerpt = False, [slide], shorten(excerpt, 240)
        elif current.planned == planned and slide not in current.slides:
            current.slides.append(slide)

    for slide in deck.slides:
        units = split_units(slide.full_text)
        for rule in RULES:
            if rule.needs_ai_context and not ai_context:
                continue
            patterns, excludes = compiled[rule.key]
            if rule.scope == "title":
                if slide.title and any(p.search(slide.title) for p in patterns):
                    record(rule, slide.number, slide.title, False)
                continue
            for unit in units:
                if excludes and any(ex.search(unit) for ex in excludes):
                    continue
                if not any(_first_affirmative(p, unit) for p in patterns):
                    continue
                planned = (rule.polarity == POS and not rule.ignore_future
                           and is_future_context(unit, slide.title, today))
                record(rule, slide.number, unit, planned)

    _numeric_signals(found, snapshot)
    return found


def _first_affirmative(pattern: re.Pattern, unit: str) -> bool:
    return any(not is_negated(unit, m.start()) for m in pattern.finditer(unit))


# Pseudo-rules for metrics read from the snapshot rather than matched as phrases.
_TEAM_RULE = _r("eo_team_size", C.ENGINEERING_ORGANIZATION, T.ENGINEERING_ORG, POS, 0.5, "engineering team size", [])
_MARGIN_RULE = _r("sc_gross_margin", C.SCALABILITY, T.ECONOMICS, POS, 0.75, "gross margin", [])
_CONCENTRATION_RULE = _r("ex_customer_concentration", C.EXIT_READINESS, T.EXIT, NEG, 0.5,
                         "customer concentration", [])
_REVENUE_RULE = _r("dm_revenue", C.DEVELOPMENT_MATURITY, T.MATURITY, POS, 0.5, "revenue-generating product", [])
RULE_INDEX.update({r.key: r for r in (_TEAM_RULE, _MARGIN_RULE, _CONCENTRATION_RULE, _REVENUE_RULE)})


def _number(value: str) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", value.replace(",", ""))
    return float(match.group()) if match else None


def _numeric_signals(found: dict[str, DetectedSignal], snap: CompanySnapshot) -> None:
    if snap.engineering_team.provided:
        count = _number(snap.engineering_team.value) or 0
        signal = DetectedSignal(_TEAM_RULE, list(snap.engineering_team.slides), snap.engineering_team.excerpt)
        if count >= 15:
            signal.label, signal.weight = f"{int(count)}-person engineering team", 1.0
        elif count >= 5:
            signal.label, signal.weight = f"{int(count)}-person engineering team", 0.5
        else:
            signal.label, signal.weight, signal.polarity = f"only {int(count)} engineer(s) identified", 0.75, NEG
            signal.flag = "SMALL_ENGINEERING_TEAM"
        found[_TEAM_RULE.key] = signal

    if snap.gross_margin.provided:
        margin = _number(snap.gross_margin.value)
        if margin is not None:
            signal = DetectedSignal(_MARGIN_RULE, list(snap.gross_margin.slides), snap.gross_margin.excerpt)
            if margin >= 70:
                signal.label = f"{snap.gross_margin.value} gross margin supports software-level cost to serve"
            elif margin >= 50:
                signal.label, signal.weight, signal.polarity = f"{snap.gross_margin.value} gross margin", 0.0, NEU
            else:
                signal.label, signal.weight, signal.polarity = f"{snap.gross_margin.value} gross margin (below software norms)", 1.0, NEG
                signal.flag = "LOW_GROSS_MARGIN"
            found[_MARGIN_RULE.key] = signal

    if snap.customer_concentration.provided:
        share = _number(snap.customer_concentration.value) or 0
        if share >= 30:
            found[_CONCENTRATION_RULE.key] = DetectedSignal(
                _CONCENTRATION_RULE, list(snap.customer_concentration.slides), snap.customer_concentration.excerpt,
                label=f"largest customer {int(share)}% of revenue", flag="CUSTOMER_CONCENTRATION")

    if snap.revenue.provided and snap.revenue.value != "Pre-revenue":
        found[_REVENUE_RULE.key] = DetectedSignal(
            _REVENUE_RULE, list(snap.revenue.slides), snap.revenue.excerpt,
            label=f"revenue-generating product ({snap.revenue.value})")


def signals_to_findings(signals: dict[str, DetectedSignal], start: int = 1) -> list[Finding]:
    findings: list[Finding] = []
    ordered = sorted(signals.values(), key=lambda s: (min(s.slides) if s.slides else 0, s.key))
    for index, signal in enumerate(ordered, start):
        rule = signal.rule
        label = signal.effective_label
        planned = signal.planned
        statement = (f"Planned, not completed: {label}" if planned else label[0].upper() + label[1:])
        findings.append(Finding(
            id=f"F{index:03d}",
            category=rule.category,
            topic=rule.topic,
            statement=statement,
            classification=Classification.INFERENCE if planned else Classification.FACT,
            polarity=NEU if planned else signal.effective_polarity,
            weight=0.0 if planned else signal.effective_weight,
            slides=signal.slides,
            excerpt=signal.excerpt,
            signal_key=rule.key,
            secondary_categories=list(rule.also),
            planned=planned,
        ))
    return findings


# --- Third-party dependencies ----------------------------------------------------

_VENDORS: tuple[tuple[str, str, str, Severity, str, bool], ...] = (
    ("AWS", r"\baws\b|amazon web services", "Cloud infrastructure", Severity.MEDIUM, "High", False),
    ("Microsoft Azure", r"\bazure\b", "Cloud infrastructure", Severity.MEDIUM, "High", False),
    ("Google Cloud", r"google cloud|\bgcp\b", "Cloud infrastructure", Severity.MEDIUM, "High", False),
    ("OpenAI", r"\bopenai\b|\bgpt-?[345](?:\.\d)?(?:o|-turbo)?\b|\bchatgpt\b", "Foundation-model API", Severity.MEDIUM, "Medium", False),
    ("Anthropic", r"\banthropic\b|\bclaude (?:api|models?)\b", "Foundation-model API", Severity.MEDIUM, "Medium", True),
    ("Google Gemini", r"\bgemini (?:api|models?|pro)\b|google gemini", "Foundation-model API", Severity.MEDIUM, "Medium", True),
    ("Meta Llama", r"\bllama ?[234]?\b", "Open-weight model", Severity.LOW, "Medium", True),
    ("Mistral", r"\bmistral\b", "Foundation-model API / weights", Severity.LOW, "Medium", True),
    ("NVIDIA", r"\bnvidia\b|\bcuda\b|\b[ah]100s?\b", "GPU compute / semiconductors", Severity.MEDIUM, "High", False),
    ("Snowflake", r"\bsnowflake\b", "Data platform", Severity.LOW, "Medium", False),
    ("Databricks", r"\bdatabricks\b", "Data platform", Severity.LOW, "Medium", False),
    ("Stripe", r"\bstripe\b", "Payment processor", Severity.LOW, "Medium", False),
    ("Plaid", r"\bplaid\b", "Financial data API", Severity.MEDIUM, "Medium", False),
    ("Twilio", r"\btwilio\b", "Telecom / messaging API", Severity.LOW, "Low", False),
    ("Salesforce", r"\bsalesforce\b", "CRM platform / marketplace", Severity.MEDIUM, "Medium", False),
    ("Epic", r"\bepic (?:systems|ehr|integration)\b", "EHR integration", Severity.MEDIUM, "High", False),
    ("Mapping platform", r"google maps|\bmapbox\b|\bhere maps\b", "Mapping platform", Severity.LOW, "Medium", False),
    ("App stores", r"\bapp store\b|google play", "App distribution", Severity.LOW, "High", False),
    ("TSMC", r"\btsmc\b", "Semiconductor foundry", Severity.HIGH, "High", False),
    ("Contract manufacturer", r"contract manufactur\w+|\bfoxconn\b|\bjabil\b|\bcelestica\b|\bsanmina\b",
     "Contract manufacturing", Severity.MEDIUM, "High", False),
    ("Freelance platform", r"\bupwork\b|\btoptal\b|\bfiverr\b", "Outsourced development", Severity.HIGH, "Medium", False),
)


def detect_dependencies(deck: DeckDocument, signals: dict[str, DetectedSignal]) -> list[ThirdPartyDependency]:
    ai_context = len(AI_CONTEXT.findall(deck.full_text)) >= 2
    dependencies: list[ThirdPartyDependency] = []
    for name, pattern, dep_type, criticality, switching, needs_ai in _VENDORS:
        if needs_ai and not ai_context:
            continue
        regex = re.compile(pattern, re.IGNORECASE)
        slides = [s.number for s in deck.slides
                  if any(not is_negated(s.full_text, m.start()) for m in regex.finditer(s.full_text))]
        if not slides:
            continue
        notes = ""
        if dep_type == "Foundation-model API" and "tpd_foundation_model" in signals:
            criticality, notes = Severity.HIGH, "Deck describes the core product as built on this provider."
        if dep_type == "Cloud infrastructure":
            if "tpd_single_cloud" in signals:
                criticality, notes = Severity.HIGH, "Deck describes exclusive reliance on one cloud."
            elif "tpd_multi_vendor" in signals:
                criticality, notes = Severity.LOW, "Deck describes multi-cloud or vendor-agnostic deployment."
        dependencies.append(ThirdPartyDependency(
            name=name, dependency_type=dep_type, criticality=criticality, switching_cost=switching,
            notes=notes, classification=Classification.FACT, slides=slides[:6]))
    return dependencies


def is_regulated_or_enterprise(deck: DeckDocument) -> bool:
    return len(REGULATED_OR_ENTERPRISE.findall(deck.full_text)) >= 2


def is_ai_company(deck: DeckDocument) -> bool:
    return len(AI_CONTEXT.findall(deck.full_text)) >= 3
