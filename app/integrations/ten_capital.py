"""TEN Capital lead-intelligence record: built locally always, transmitted only with consent.

Consent is an explicit, per-run decision made by the person running the
analysis. It is represented by `ConsentStatus` and can only become GRANTED
through `resolve_consent(True)` — a literal boolean `True` from an unticked-by-
default checkbox or an explicit CLI flag. Strings, environment variables and
defaults never grant consent, and `submit_record` refuses to open a network
connection for any record whose status is not GRANTED.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from app.config import Settings
from app.models.schemas import NOT_PROVIDED, DiligenceAnalysis, ScoreCategory as C, Severity
from app.utils.logging import get_logger
from app.utils.text import shorten

log = get_logger("integrations.ten_capital")

RECORD_TYPE = "technical_ip_diligence_lead"
RECORD_SCHEMA_VERSION = "1.0"


class ConsentStatus(StrEnum):
    GRANTED = "GRANTED"
    NOT_GRANTED = "NOT_GRANTED"


class TransmissionStatus(StrEnum):
    NOT_SENT_NO_CONSENT = "NOT_SENT_NO_CONSENT"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    SENT = "SENT"
    FAILED = "FAILED"
    PENDING = "PENDING"


def resolve_consent(explicit: object) -> ConsentStatus:
    """Only a literal boolean True grants consent. Anything else — including "true" — does not."""
    return ConsentStatus.GRANTED if explicit is True else ConsentStatus.NOT_GRANTED


AREA_BY_CATEGORY: dict[C, str] = {
    C.IP_OWNERSHIP: "IP chain of title: invention assignments, contractor agreements, open-source licenses (IP counsel)",
    C.IP_DEFENSIBILITY: "Patent and trade-secret mapping to the revenue product (IP counsel)",
    C.TECHNICAL_ARCHITECTURE: "Architecture and infrastructure review incl. single points of failure (technical advisor)",
    C.DEVELOPMENT_MATURITY: "Production readiness: CI/CD, testing, reliability and DR evidence (technical advisor)",
    C.SCALABILITY: "10x load and cost-to-serve review (technical advisor / finance)",
    C.ENGINEERING_ORGANIZATION: "Engineering organization, key-person and succession review (CTO advisor)",
    C.TECHNICAL_DOCUMENTATION: "Code and documentation audit; technical-debt sizing (independent code reviewer)",
    C.CYBERSECURITY_COMPLIANCE: "Security and compliance: attestations, penetration tests, incident response (CISO)",
    C.THIRD_PARTY_DEPENDENCY: "Third-party dependency, license and change-of-control review (CTO / counsel)",
    C.EXIT_READINESS: "Transaction readiness: data room, third-party consents, escrow exposure (deal counsel)",
}


class TenCapitalRecord(BaseModel):
    record_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    record_type: str = RECORD_TYPE
    schema_version: str = RECORD_SCHEMA_VERSION
    user: str
    company: str
    sector: str
    stage: str
    raise_amount: str
    revenue: str
    growth: str
    gross_margin: str
    overall_technical_ip_score: int
    overall_score_uncertainty: int
    evidence_confidence: int
    technical_architecture_rating: int
    development_maturity_rating: int
    development_maturity_level: str
    scalability_rating: int
    engineering_organization_rating: int
    technical_documentation_rating: int
    cybersecurity_rating: int
    ip_ownership_rating: int
    ip_defensibility_rating: int
    third_party_dependency_rating: int
    transaction_readiness_rating: int
    transaction_readiness: str
    technical_position: str
    ip_position: str
    critical_red_flags: list[str]
    high_severity_red_flags: list[str]
    top_diligence_questions: list[str]
    major_missing_information: list[str]
    summary_findings: list[str]
    recommended_diligence_areas: list[str]
    consent_status: ConsentStatus
    transmission_status: TransmissionStatus = TransmissionStatus.PENDING
    report_created_at: datetime
    source_file: str
    source_sha256: str
    analysis_engine: str


class SubmissionResult(BaseModel):
    status: TransmissionStatus
    detail: str
    http_status: int | None = None


def recommended_areas(analysis: DiligenceAnalysis, limit: int = 5) -> list[str]:
    flagged = [f.category for f in analysis.red_flags if f.severity in (Severity.CRITICAL, Severity.HIGH)]
    weak = [c.category for c in sorted(analysis.scores.categories, key=lambda c: (c.score, -c.weight)) if c.score <= 3]
    ordered: list[C] = []
    for category in flagged + weak:
        if category not in ordered:
            ordered.append(category)
    return [AREA_BY_CATEGORY[c] for c in ordered[:limit]]


def build_record(analysis: DiligenceAnalysis, *, user: str | None, consent: ConsentStatus) -> TenCapitalRecord:
    if not isinstance(consent, ConsentStatus):
        raise TypeError("consent must be a ConsentStatus; use resolve_consent() with an explicit user choice")
    snap = analysis.company
    score = {c.category: c.score for c in analysis.scores.categories}
    conclusion = analysis.investor_conclusion

    def value(field) -> str:
        return field.value if field.provided else NOT_PROVIDED

    return TenCapitalRecord(
        user=(user or "").strip() or NOT_PROVIDED,
        company=snap.display_name(),
        sector=value(snap.sector), stage=value(snap.stage), raise_amount=value(snap.raise_amount),
        revenue=value(snap.revenue), growth=value(snap.growth), gross_margin=value(snap.gross_margin),
        overall_technical_ip_score=analysis.scores.overall_score,
        overall_score_uncertainty=analysis.scores.uncertainty,
        evidence_confidence=analysis.evidence_confidence,
        technical_architecture_rating=score[C.TECHNICAL_ARCHITECTURE],
        development_maturity_rating=score[C.DEVELOPMENT_MATURITY],
        development_maturity_level=analysis.development_maturity.level.value,
        scalability_rating=score[C.SCALABILITY],
        engineering_organization_rating=score[C.ENGINEERING_ORGANIZATION],
        technical_documentation_rating=score[C.TECHNICAL_DOCUMENTATION],
        cybersecurity_rating=score[C.CYBERSECURITY_COMPLIANCE],
        ip_ownership_rating=score[C.IP_OWNERSHIP],
        ip_defensibility_rating=score[C.IP_DEFENSIBILITY],
        third_party_dependency_rating=score[C.THIRD_PARTY_DEPENDENCY],
        transaction_readiness_rating=score[C.EXIT_READINESS],
        transaction_readiness=conclusion.transaction_readiness.value,
        technical_position=conclusion.technical_position.value,
        ip_position=conclusion.ip_position.value,
        critical_red_flags=[f.issue for f in analysis.flags_at(Severity.CRITICAL)],
        high_severity_red_flags=[f.issue for f in analysis.flags_at(Severity.HIGH)],
        top_diligence_questions=[q.question for q in analysis.diligence_questions[:5]],
        major_missing_information=[m.item for m in analysis.missing_information
                                   if m.importance in (Severity.CRITICAL, Severity.HIGH)][:10],
        summary_findings=[
            conclusion.summary,
            f"Technical: {shorten(analysis.technical_architecture.summary, 200)}",
            f"IP: {shorten(analysis.intellectual_property.ownership, 200)}",
        ],
        recommended_diligence_areas=recommended_areas(analysis),
        consent_status=consent,
        transmission_status=(TransmissionStatus.PENDING if consent is ConsentStatus.GRANTED
                             else TransmissionStatus.NOT_SENT_NO_CONSENT),
        report_created_at=analysis.generated_at,
        source_file=analysis.source.filename,
        source_sha256=analysis.source.sha256,
        analysis_engine=analysis.engine.name,
    )


def submit_record(record: TenCapitalRecord, settings: Settings,
                  transport: httpx.BaseTransport | None = None) -> SubmissionResult:
    """Send the record to TEN Capital. Never touches the network without GRANTED consent."""
    if record.consent_status is not ConsentStatus.GRANTED:
        log.info("TEN Capital record %s kept local: consent not granted", record.record_id)
        return SubmissionResult(status=TransmissionStatus.NOT_SENT_NO_CONSENT,
                                detail="Consent not granted; the record was saved locally and not transmitted.")
    endpoint = settings.ten_capital_endpoint
    if not endpoint:
        return SubmissionResult(status=TransmissionStatus.NOT_CONFIGURED,
                                detail="Consent granted, but TDIP_TEN_CAPITAL_ENDPOINT is not configured.")
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        return SubmissionResult(status=TransmissionStatus.FAILED, detail="The TEN Capital endpoint must use HTTPS.")

    headers = {"Content-Type": "application/json", "Idempotency-Key": record.record_id,
               "User-Agent": "ten-capital-tech-ip-diligence/1.0"}
    if settings.ten_capital_api_key:
        headers["Authorization"] = f"Bearer {settings.ten_capital_api_key.get_secret_value()}"
    payload = record.model_dump(mode="json", exclude={"transmission_status"})
    try:
        with httpx.Client(timeout=settings.ten_capital_timeout_seconds, transport=transport) as client:
            response = client.post(endpoint, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        log.error("TEN Capital submission failed: %s", exc)
        return SubmissionResult(status=TransmissionStatus.FAILED, detail=f"Network error: {type(exc).__name__}")
    if 200 <= response.status_code < 300:
        log.info("TEN Capital record %s sent (HTTP %s)", record.record_id, response.status_code)
        return SubmissionResult(status=TransmissionStatus.SENT, detail="Record delivered to TEN Capital.",
                                http_status=response.status_code)
    log.error("TEN Capital endpoint returned HTTP %s", response.status_code)
    return SubmissionResult(status=TransmissionStatus.FAILED, detail=f"Endpoint returned HTTP {response.status_code}.",
                            http_status=response.status_code)


def write_record(record: TenCapitalRecord, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.model_dump(mode="json"), indent=2), encoding="utf-8")
    return path


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
