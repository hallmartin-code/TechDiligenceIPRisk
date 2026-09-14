"""Structural and reliability validation run before any report is generated.

Pydantic enforces types and ranges; these checks enforce the cross-field rules
the report depends on: every FACT is traceable to a real slide, scores add up,
every section reference resolves, and nothing marked NOT PROVIDED carries a value.
"""

from __future__ import annotations

from app.analysis.risk_scoring import overall_from_average, weighted_average
from app.models.schemas import NOT_PROVIDED, Classification, DiligenceAnalysis, ScoreCategory


class AnalysisValidationError(ValueError):
    def __init__(self, issues: list[str]) -> None:
        super().__init__("Analysis failed validation: " + "; ".join(issues[:10]))
        self.issues = issues


def validate_analysis(analysis: DiligenceAnalysis) -> list[str]:
    """Raise `AnalysisValidationError` on errors; return non-fatal warnings."""
    errors: list[str] = []
    warnings: list[str] = []
    slide_count = analysis.source.slide_count

    categories = [c.category for c in analysis.scores.categories]
    if sorted(categories) != sorted(ScoreCategory):
        errors.append("Scores must include each of the ten categories exactly once.")
    total_weight = sum(c.weight for c in analysis.scores.categories)
    if abs(total_weight - 1.0) > 1e-6:
        errors.append(f"Category weights sum to {total_weight:.4f}, not 1.0.")
    if analysis.scores.categories:
        expected = overall_from_average(weighted_average(analysis.scores.categories))
        if abs(expected - analysis.scores.overall_score) > 1:
            errors.append(f"Overall score {analysis.scores.overall_score} does not match weighted scores ({expected}).")

    ids = [f.id for f in analysis.source_evidence]
    if len(ids) != len(set(ids)):
        errors.append("Finding ids are not unique.")
    known = set(ids)
    for finding in analysis.source_evidence:
        if any(s > slide_count for s in finding.slides):
            errors.append(f"{finding.id} cites a slide beyond the deck ({finding.slides}).")
        if finding.classification == Classification.FACT and (not finding.slides or not finding.excerpt):
            errors.append(f"{finding.id} is a FACT without a slide reference and excerpt.")
        if finding.planned and finding.weight > 0:
            errors.append(f"{finding.id} is a planned item but carries score weight.")

    sections = {
        "technical_architecture": analysis.technical_architecture, "development_maturity": analysis.development_maturity,
        "engineering_organization": analysis.engineering_organization,
        "technical_documentation": analysis.technical_documentation, "cybersecurity": analysis.cybersecurity,
        "intellectual_property": analysis.intellectual_property, "ai_data": analysis.ai_data,
        "scalability": analysis.scalability, "exit_readiness": analysis.exit_readiness,
    }
    for name, section in sections.items():
        unknown = [i for i in section.finding_ids if i not in known]
        if unknown:
            errors.append(f"Section {name} references unknown findings {unknown[:3]}.")
        if not section.summary.strip():
            errors.append(f"Section {name} has an empty summary.")

    for flag in analysis.red_flags:
        if any(s < 1 or s > slide_count for s in flag.slides):
            errors.append(f"Red flag {flag.code} cites an invalid slide ({flag.slides}).")
        if flag.classification == Classification.FACT and not flag.slides:
            errors.append(f"Red flag {flag.code} is a FACT without slide references.")

    for name, field in analysis.company:
        if field.classification == Classification.NOT_PROVIDED and field.value != NOT_PROVIDED:
            errors.append(f"Snapshot field {name} is NOT PROVIDED but carries a value.")
        if field.classification == Classification.FACT and not field.slides:
            errors.append(f"Snapshot field {name} is a FACT without a slide reference.")
        if any(s < 1 or s > slide_count for s in field.slides):
            errors.append(f"Snapshot field {name} cites an invalid slide.")

    for dependency in analysis.third_party_dependencies:
        if any(s < 1 or s > slide_count for s in dependency.slides):
            errors.append(f"Dependency {dependency.name} cites an invalid slide.")

    if not analysis.diligence_questions:
        errors.append("At least one diligence question is required.")
    elif len(analysis.diligence_questions) < 5:
        warnings.append("Fewer than five diligence questions were generated.")
    priorities = [q.priority for q in analysis.diligence_questions]
    if priorities != list(range(1, len(priorities) + 1)):
        errors.append("Diligence question priorities must run 1..n in order.")

    if errors:
        raise AnalysisValidationError(errors)
    return warnings
