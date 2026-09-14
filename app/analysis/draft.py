"""Engine-neutral intermediate result, turned into a `DiligenceAnalysis` by `engine.finalize`."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.schemas import (
    AIDataAssessment,
    Assumption,
    CompanySnapshot,
    DevelopmentMaturityAssessment,
    DiligenceQuestion,
    EngineeringOrganizationAssessment,
    EngineInfo,
    Finding,
    IntellectualPropertyAssessment,
    MissingInformation,
    RedFlag,
    ScalabilityAssessment,
    ScoreCategory,
    SectionAssessment,
    TechnicalArchitectureAssessment,
    ThirdPartyDependency,
)


@dataclass
class AnalysisDraft:
    engine: EngineInfo
    snapshot: CompanySnapshot
    findings: list[Finding]
    technical_architecture: TechnicalArchitectureAssessment
    development_maturity: DevelopmentMaturityAssessment
    engineering_organization: EngineeringOrganizationAssessment
    technical_documentation: SectionAssessment
    cybersecurity: SectionAssessment
    intellectual_property: IntellectualPropertyAssessment
    ai_data: AIDataAssessment
    scalability: ScalabilityAssessment
    dependencies: list[ThirdPartyDependency]
    flags: list[RedFlag]
    proposed_scores: dict[ScoreCategory, tuple[int, str]] | None = None
    questions: list[DiligenceQuestion] = field(default_factory=list)
    assumptions: list[Assumption] = field(default_factory=list)
    missing: list[MissingInformation] = field(default_factory=list)
    conclusion_summary: str | None = None
    exit_summary: str | None = None
    warnings: list[str] = field(default_factory=list)
    grounding_rejections: int = 0
