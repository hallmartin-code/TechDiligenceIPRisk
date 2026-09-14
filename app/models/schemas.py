"""Typed schema for the Technical & IP Diligence analysis.

Every material finding carries its source slide(s), the excerpt it rests on,
and a classification of FACT / INFERENCE / ASSUMPTION / NOT PROVIDED. The PDF,
the JSON export and the TEN Capital record are all rendered from one validated
`DiligenceAnalysis` object, so the three outputs cannot disagree.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "1.0"
NOT_PROVIDED = "NOT PROVIDED"


class Classification(StrEnum):
    FACT = "FACT"
    INFERENCE = "INFERENCE"
    ASSUMPTION = "ASSUMPTION"
    NOT_PROVIDED = "NOT PROVIDED"


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


SEVERITY_RANK = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}


class Polarity(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"


class ScoreCategory(StrEnum):
    TECHNICAL_ARCHITECTURE = "technical_architecture"
    DEVELOPMENT_MATURITY = "development_maturity"
    SCALABILITY = "scalability"
    ENGINEERING_ORGANIZATION = "engineering_organization"
    TECHNICAL_DOCUMENTATION = "technical_documentation"
    CYBERSECURITY_COMPLIANCE = "cybersecurity_compliance"
    IP_OWNERSHIP = "ip_ownership"
    IP_DEFENSIBILITY = "ip_defensibility"
    THIRD_PARTY_DEPENDENCY = "third_party_dependency"
    EXIT_READINESS = "exit_readiness"


CATEGORY_LABELS: dict[ScoreCategory, str] = {
    ScoreCategory.TECHNICAL_ARCHITECTURE: "Technical Architecture",
    ScoreCategory.DEVELOPMENT_MATURITY: "Product / Development Maturity",
    ScoreCategory.SCALABILITY: "Scalability",
    ScoreCategory.ENGINEERING_ORGANIZATION: "Engineering Organization",
    ScoreCategory.TECHNICAL_DOCUMENTATION: "Technical Documentation",
    ScoreCategory.CYBERSECURITY_COMPLIANCE: "Cybersecurity / Compliance",
    ScoreCategory.IP_OWNERSHIP: "IP Ownership",
    ScoreCategory.IP_DEFENSIBILITY: "IP Defensibility",
    ScoreCategory.THIRD_PARTY_DEPENDENCY: "Third-Party Dependency",
    ScoreCategory.EXIT_READINESS: "Exit / Transaction Readiness",
}

CATEGORY_SHORT_LABELS: dict[ScoreCategory, str] = {
    ScoreCategory.TECHNICAL_ARCHITECTURE: "Architecture",
    ScoreCategory.DEVELOPMENT_MATURITY: "Maturity",
    ScoreCategory.SCALABILITY: "Scalability",
    ScoreCategory.ENGINEERING_ORGANIZATION: "Eng. Org",
    ScoreCategory.TECHNICAL_DOCUMENTATION: "Docs",
    ScoreCategory.CYBERSECURITY_COMPLIANCE: "Security",
    ScoreCategory.IP_OWNERSHIP: "IP Owner.",
    ScoreCategory.IP_DEFENSIBILITY: "IP Defens.",
    ScoreCategory.THIRD_PARTY_DEPENDENCY: "3rd Party",
    ScoreCategory.EXIT_READINESS: "Exit Ready",
}

TECHNICAL_CATEGORIES = (
    ScoreCategory.TECHNICAL_ARCHITECTURE,
    ScoreCategory.DEVELOPMENT_MATURITY,
    ScoreCategory.SCALABILITY,
    ScoreCategory.ENGINEERING_ORGANIZATION,
    ScoreCategory.TECHNICAL_DOCUMENTATION,
    ScoreCategory.CYBERSECURITY_COMPLIANCE,
)
IP_CATEGORIES = (
    ScoreCategory.IP_OWNERSHIP,
    ScoreCategory.IP_DEFENSIBILITY,
    ScoreCategory.THIRD_PARTY_DEPENDENCY,
)


class Topic(StrEnum):
    COMPANY = "company"
    ARCHITECTURE = "architecture"
    MATURITY = "maturity"
    SCALABILITY = "scalability"
    ECONOMICS = "economics"
    ENGINEERING_ORG = "engineering_org"
    DOCUMENTATION = "documentation"
    SECURITY = "security"
    COMPLIANCE = "compliance"
    PATENTS = "patents"
    TRADE_SECRETS = "trade_secrets"
    SOFTWARE_IP = "software_ip"
    OWNERSHIP = "ownership"
    AI_DATA = "ai_data"
    DEPENDENCY = "dependency"
    EXIT = "exit"


class MaturityLevel(StrEnum):
    EXPERIMENTAL = "Experimental"
    PROTOTYPE = "Prototype"
    EARLY_PRODUCTION = "Early Production"
    PRODUCTION = "Production"
    SCALED_PRODUCTION = "Scaled Production"
    ENTERPRISE_GRADE = "Enterprise-Grade"
    NOT_DETERMINABLE = "Not determinable"


class PositionRating(StrEnum):
    STRONG = "Strong"
    ADEQUATE = "Adequate"
    MIXED = "Mixed"
    WEAK = "Weak"
    HIGH_RISK = "High Risk"


class TransactionReadiness(StrEnum):
    READY = "Ready"
    MOSTLY_READY = "Mostly Ready"
    MATERIAL_DILIGENCE = "Material Diligence Required"
    SIGNIFICANT_REMEDIATION = "Significant Remediation Required"


class TransactionExposure(StrEnum):
    DILIGENCE_QUESTION = "Diligence question"
    CLOSING_CONDITION = "Closing condition"
    REPS_AND_WARRANTIES = "Reps & warranties"
    INDEMNITY = "Indemnity"
    ESCROW = "Escrow / holdback"
    PURCHASE_PRICE_ADJUSTMENT = "Purchase-price adjustment"
    DELAY_OR_TERMINATION = "Delay / termination risk"


class EvidenceStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    LIMITED = "LIMITED"
    NOT_PROVIDED = "NOT PROVIDED"


class _Model(BaseModel):
    model_config = ConfigDict(use_enum_values=False, validate_assignment=True)


# --- Source ------------------------------------------------------------------


class SourceDocument(_Model):
    filename: str
    file_type: str
    slide_count: int = Field(ge=0)
    sha256: str
    text_characters: int = Field(ge=0)
    image_only_slides: list[int] = Field(default_factory=list)
    parse_warnings: list[str] = Field(default_factory=list)


class EngineInfo(_Model):
    name: str  # "llm" or "rules"
    model: str | None = None
    fallback_reason: str | None = None


# --- Evidence ----------------------------------------------------------------


class Finding(_Model):
    id: str
    category: ScoreCategory
    topic: Topic
    statement: str = Field(min_length=1)
    classification: Classification
    polarity: Polarity = Polarity.NEUTRAL
    weight: float = Field(default=0.0, ge=0.0, le=3.0)
    slides: list[int] = Field(default_factory=list)
    excerpt: str = ""
    signal_key: str | None = None
    secondary_categories: list[ScoreCategory] = Field(default_factory=list)
    planned: bool = False

    @field_validator("slides")
    @classmethod
    def _sorted_unique(cls, value: list[int]) -> list[int]:
        return sorted({s for s in value if s >= 1})


class SnapshotField(_Model):
    value: str = NOT_PROVIDED
    classification: Classification = Classification.NOT_PROVIDED
    slides: list[int] = Field(default_factory=list)
    excerpt: str = ""

    @property
    def provided(self) -> bool:
        return self.classification != Classification.NOT_PROVIDED and self.value != NOT_PROVIDED


class CompanySnapshot(_Model):
    name: SnapshotField = Field(default_factory=SnapshotField)
    sector: SnapshotField = Field(default_factory=SnapshotField)
    stage: SnapshotField = Field(default_factory=SnapshotField)
    product: SnapshotField = Field(default_factory=SnapshotField)
    revenue: SnapshotField = Field(default_factory=SnapshotField)
    growth: SnapshotField = Field(default_factory=SnapshotField)
    gross_margin: SnapshotField = Field(default_factory=SnapshotField)
    customers: SnapshotField = Field(default_factory=SnapshotField)
    customer_concentration: SnapshotField = Field(default_factory=SnapshotField)
    funding_to_date: SnapshotField = Field(default_factory=SnapshotField)
    raise_amount: SnapshotField = Field(default_factory=SnapshotField)
    engineering_team: SnapshotField = Field(default_factory=SnapshotField)

    def display_name(self, fallback: str = "Unnamed company") -> str:
        return self.name.value if self.name.provided else fallback


# --- Sections ----------------------------------------------------------------


class SectionAssessment(_Model):
    summary: str = NOT_PROVIDED
    finding_ids: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class TechnicalArchitectureAssessment(SectionAssessment):
    risk_points: list[str] = Field(default_factory=list)


class DevelopmentMaturityAssessment(SectionAssessment):
    level: MaturityLevel = MaturityLevel.NOT_DETERMINABLE
    rationale: str = NOT_PROVIDED


class EngineeringOrganizationAssessment(SectionAssessment):
    key_person_risk: bool = False


class ScalabilityAssessment(SectionAssessment):
    economic_risks: list[str] = Field(default_factory=list)


class IntellectualPropertyAssessment(SectionAssessment):
    core_ip: str = NOT_PROVIDED
    ownership: str = NOT_PROVIDED
    defensibility: str = NOT_PROVIDED
    patents: str = NOT_PROVIDED
    trade_secrets: str = NOT_PROVIDED
    software_data: str = NOT_PROVIDED
    third_party: str = NOT_PROVIDED


class AIDataAssessment(SectionAssessment):
    applicable: bool = False
    proprietary_assessment: str = NOT_PROVIDED


class ThirdPartyDependency(_Model):
    name: str
    dependency_type: str
    criticality: Severity = Severity.MEDIUM
    switching_cost: str = "Unknown"
    notes: str = ""
    classification: Classification = Classification.FACT
    slides: list[int] = Field(default_factory=list)


class ExposureItem(_Model):
    issue: str
    severity: Severity
    exposure_types: list[TransactionExposure] = Field(default_factory=list)
    flag_code: str | None = None


class ExitReadinessAssessment(SectionAssessment):
    exposures: list[ExposureItem] = Field(default_factory=list)


# --- Scores, flags, questions -------------------------------------------------


class CategoryScore(_Model):
    category: ScoreCategory
    label: str
    score: int = Field(ge=1, le=5)
    weight: float = Field(ge=0.0, le=1.0)
    rationale: str
    evidence_status: EvidenceStatus
    caps_applied: list[str] = Field(default_factory=list)


class ScoreSummary(_Model):
    categories: list[CategoryScore]
    weighted_average: float = Field(ge=1.0, le=5.0)
    overall_score: int = Field(ge=0, le=100)
    uncertainty: int = Field(ge=0, le=50)

    def get(self, category: ScoreCategory) -> CategoryScore:
        for item in self.categories:
            if item.category == category:
                return item
        raise KeyError(category)


class RedFlag(_Model):
    code: str
    category: ScoreCategory
    issue: str = Field(min_length=1)
    severity: Severity
    classification: Classification
    evidence: str
    slides: list[int] = Field(default_factory=list)
    business_consequence: str = Field(min_length=1)
    diligence_question: str = Field(min_length=1)
    mitigation: str = Field(min_length=1)
    exposures: list[TransactionExposure] = Field(default_factory=list)


class DiligenceQuestion(_Model):
    priority: int = Field(ge=1)
    question: str = Field(min_length=1)
    audience: str
    rationale: str
    category: ScoreCategory
    flag_code: str | None = None


class Assumption(_Model):
    statement: str
    classification: Classification
    basis: str = ""


class MissingInformation(_Model):
    item: str
    category: ScoreCategory
    importance: Severity = Severity.MEDIUM


class InvestorConclusion(_Model):
    technical_position: PositionRating
    ip_position: PositionRating
    transaction_readiness: TransactionReadiness
    summary: str = Field(min_length=1)


# --- Root --------------------------------------------------------------------


class DiligenceAnalysis(_Model):
    schema_version: str = SCHEMA_VERSION
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: SourceDocument
    engine: EngineInfo
    company: CompanySnapshot
    source_evidence: list[Finding] = Field(default_factory=list)
    technical_architecture: TechnicalArchitectureAssessment
    development_maturity: DevelopmentMaturityAssessment
    engineering_organization: EngineeringOrganizationAssessment
    technical_documentation: SectionAssessment
    cybersecurity: SectionAssessment
    intellectual_property: IntellectualPropertyAssessment
    ai_data: AIDataAssessment
    third_party_dependencies: list[ThirdPartyDependency] = Field(default_factory=list)
    scalability: ScalabilityAssessment
    exit_readiness: ExitReadinessAssessment
    scores: ScoreSummary
    evidence_confidence: int = Field(ge=0, le=100)
    red_flags: list[RedFlag] = Field(default_factory=list)
    diligence_questions: list[DiligenceQuestion] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    missing_information: list[MissingInformation] = Field(default_factory=list)
    investor_conclusion: InvestorConclusion
    warnings: list[str] = Field(default_factory=list)

    def finding(self, finding_id: str) -> Finding | None:
        return next((f for f in self.source_evidence if f.id == finding_id), None)

    def findings_for(self, section: SectionAssessment) -> list[Finding]:
        index = {f.id: f for f in self.source_evidence}
        return [index[i] for i in section.finding_ids if i in index]

    def flags_at(self, *severities: Severity) -> list[RedFlag]:
        return [flag for flag in self.red_flags if flag.severity in severities]

    def classification_counts(self) -> dict[str, int]:
        counts = {c.value: 0 for c in Classification}
        for finding in self.source_evidence:
            counts[finding.classification.value] += 1
        counts[Classification.NOT_PROVIDED.value] += len(self.missing_information)
        return counts
