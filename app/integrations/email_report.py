"""Automated result emails: every completed analysis is sent to the TEN Capital inbox via Resend.

Each email carries the conclusion itself — score, ratings, top risks, diligence
questions, snapshot and run details — with the one-page PDF and the structured
analysis JSON attached, so the team can triage from the inbox.

Uploader privacy follows the consent checkbox: the uploader's email address, a
reply-to header and the TEN Capital lead record are included only when consent
was explicitly GRANTED for that run.

Delivery is best-effort. A failed send is logged and reported in the result,
but never costs the user the analysis that was just produced.
"""

from __future__ import annotations

import base64
import html
import re
from enum import StrEnum
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from app.config import Settings
from app.integrations.ten_capital import ConsentStatus, TenCapitalRecord
from app.models.schemas import DiligenceAnalysis, Severity, SnapshotField
from app.utils.logging import get_logger
from app.utils.text import shorten

log = get_logger("integrations.email")

RESEND_ENDPOINT = "https://api.resend.com/emails"
_EMAIL_ADDRESS = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
_FONT = "Helvetica,Arial,sans-serif"

_SEVERITY_STYLE = {
    Severity.CRITICAL: ("#FDECEA", "#B42318"),
    Severity.HIGH: ("#FDEEE6", "#C2410C"),
    Severity.MEDIUM: ("#FDF3E3", "#8A5314"),
    Severity.LOW: ("#EEF2F6", "#5B7083"),
}
_RATING_COLOR = {
    "Strong": "#1F7A4D", "Adequate": "#3F8A3A", "Mixed": "#A86B12", "Weak": "#D9531E", "High Risk": "#B42318",
    "Ready": "#1F7A4D", "Mostly Ready": "#3F8A3A", "Material Diligence Required": "#A86B12",
    "Significant Remediation Required": "#B42318",
}


_TRANSMISSION_LABELS = {
    "NOT_SENT_NO_CONSENT": "Not sent (no consent)",
    "NOT_CONFIGURED": "Consent granted; intake endpoint not configured",
    "SENT": "Sent to TEN Capital intake",
    "FAILED": "Transmission failed",
    "PENDING": "Pending",
}


class EmailStatus(StrEnum):
    SENT = "SENT"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    DISABLED = "DISABLED"
    FAILED = "FAILED"


class EmailResult(BaseModel):
    status: EmailStatus
    detail: str
    message_id: str | None = None
    recipients: list[str] = Field(default_factory=list)


def recipients(settings: Settings) -> list[str]:
    return [address.strip() for address in settings.report_email_to.split(",") if address.strip()]


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _consented(record: TenCapitalRecord) -> bool:
    return record.consent_status is ConsentStatus.GRANTED


def _uploader(record: TenCapitalRecord) -> str:
    if not _consented(record):
        return "Withheld (consent not granted)"
    return record.user


def _score_color(score: int) -> str:
    return "#1F7A4D" if score >= 70 else "#A86B12" if score >= 45 else "#B42318"


def _engine_label(analysis: DiligenceAnalysis) -> str:
    engine = analysis.engine
    label = f"Claude ({engine.model})" if engine.name == "llm" else "Deterministic rules"
    return f"{label}; {engine.fallback_reason}" if engine.fallback_reason else label


def _field(field: SnapshotField) -> str:
    if not field.provided:
        return "Not provided"
    return field.value + (" (inferred)" if field.classification.value in ("INFERENCE", "ASSUMPTION") else "")


def _snapshot_rows(analysis: DiligenceAnalysis) -> list[tuple[str, str]]:
    snap = analysis.company
    return [
        ("Sector", _field(snap.sector)), ("Stage", _field(snap.stage)), ("Revenue", _field(snap.revenue)),
        ("Growth", _field(snap.growth)), ("Gross margin", _field(snap.gross_margin)),
        ("Customers", _field(snap.customers)), ("Concentration", _field(snap.customer_concentration)),
        ("Raise", _field(snap.raise_amount)), ("Funding to date", _field(snap.funding_to_date)),
        ("Engineering team", _field(snap.engineering_team)),
        ("Development maturity", analysis.development_maturity.level.value),
    ]


def _run_rows(analysis: DiligenceAnalysis, record: TenCapitalRecord) -> list[tuple[str, str]]:
    counts = analysis.classification_counts()
    source = analysis.source
    return [
        ("Uploader", _uploader(record)),
        ("Contact consent", "Granted" if _consented(record) else "Not granted"),
        ("Lead record", _TRANSMISSION_LABELS.get(record.transmission_status.value,
                                                 record.transmission_status.value)),
        ("Engine", _engine_label(analysis)),
        ("Source", f"{source.filename} ({source.slide_count} slides) · sha256 {source.sha256[:12]}"),
        ("Generated", analysis.generated_at.strftime("%Y-%m-%d %H:%M UTC")),
        ("Evidence basis", f"{counts['FACT']} facts · {counts['INFERENCE']} inferences · "
                           f"{counts['ASSUMPTION']} assumptions · {counts['NOT PROVIDED']} not provided"),
        ("Record ID", record.record_id),
    ]


def build_subject(analysis: DiligenceAnalysis) -> str:
    conclusion = analysis.investor_conclusion
    parts = [f"Tech & IP Review — {shorten(analysis.company.display_name(), 60)}",
             f"{analysis.scores.overall_score} ±{analysis.scores.uncertainty}",
             conclusion.transaction_readiness.value]
    critical = len(analysis.flags_at(Severity.CRITICAL))
    if critical:
        parts.append(f"{critical} critical")
    return " · ".join(parts)


def build_text(analysis: DiligenceAnalysis, record: TenCapitalRecord) -> str:
    conclusion = analysis.investor_conclusion
    lines = [
        build_subject(analysis), "",
        f"Tech & IP score: {analysis.scores.overall_score} ±{analysis.scores.uncertainty}   "
        f"Evidence confidence: {analysis.evidence_confidence}/100",
        f"Technical: {conclusion.technical_position.value}   IP: {conclusion.ip_position.value}   "
        f"Transaction readiness: {conclusion.transaction_readiness.value}", "",
        "CONCLUSION", conclusion.summary, "", "TOP RISKS",
    ]
    for flag in analysis.red_flags[:6]:
        lines.append(f"[{flag.severity.value}] {flag.issue} ({flag.classification.value}; {shorten(flag.evidence, 140)})")
    lines += ["", "PRIORITY DILIGENCE QUESTIONS"]
    lines += [f"{q.priority}. {q.question} [{q.audience}]" for q in analysis.diligence_questions[:5]]
    lines += ["", "COMPANY SNAPSHOT"] + [f"{label}: {value}" for label, value in _snapshot_rows(analysis)]
    lines += ["", "RUN DETAILS"] + [f"{label}: {value}" for label, value in _run_rows(analysis, record)]
    if analysis.warnings:
        lines += ["", "WARNINGS"] + [f"- {w}" for w in analysis.warnings[:5]]
    lines += ["", "The one-page PDF and the structured analysis JSON are attached.",
              "Deck-based screening only; not legal advice, a patent-validity or freedom-to-operate opinion, "
              "or a security audit.", "TEN Capital Network"]
    return "\n".join(lines)


def build_html(analysis: DiligenceAnalysis, record: TenCapitalRecord) -> str:
    """Brand-styled, table-based HTML with inline styles (email clients strip <style> and flexbox)."""
    conclusion = analysis.investor_conclusion
    snap = analysis.company
    meta = " · ".join([v.value for v in (snap.sector, snap.stage) if v.provided]
                      + [f"{analysis.source.filename} ({analysis.source.slide_count} slides)"])

    def cell(label: str, value: str, color: str, suffix: str = "") -> str:
        suffix_html = (f'<span style="font:600 11px/1 {_FONT};color:#7E90A8;"> {_e(suffix)}</span>'
                       if suffix else "")
        return (f'<td width="33%" style="padding:12px 12px;vertical-align:top;border:1px solid #E3E8EF;">'
                f'<div style="font:600 9px/1.4 {_FONT};letter-spacing:.12em;text-transform:uppercase;'
                f'color:#7E90A8;">{_e(label)}</div>'
                f'<div style="font:700 16px/1.3 {_FONT};color:{color};margin-top:4px;">{_e(value)}{suffix_html}</div>'
                f"</td>")

    scores = analysis.scores
    metrics = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;margin:0 0 16px;">'
        "<tr>"
        + cell("Tech & IP score", str(scores.overall_score), _score_color(scores.overall_score), f"±{scores.uncertainty}")
        + cell("Evidence confidence", str(analysis.evidence_confidence), "#0B1526", "/100")
        + cell("Development maturity", analysis.development_maturity.level.value, "#0B1526")
        + "</tr><tr>"
        + cell("Technical position", conclusion.technical_position.value,
               _RATING_COLOR.get(conclusion.technical_position.value, "#0B1526"))
        + cell("IP position", conclusion.ip_position.value, _RATING_COLOR.get(conclusion.ip_position.value, "#0B1526"))
        + cell("Transaction readiness", conclusion.transaction_readiness.value,
               _RATING_COLOR.get(conclusion.transaction_readiness.value, "#0B1526"))
        + "</tr></table>"
    )

    conclusion_box = (
        f'<div style="padding:13px 15px;background:#F3F6FA;border-left:3px solid #EE5A4E;'
        f'font:400 13px/1.6 {_FONT};color:#1F2733;">{_e(conclusion.summary)}</div>'
    )

    def section(title: str, body: str) -> str:
        return ('<tr><td style="padding:22px 26px 0;">'
                f'<div style="font:700 10px/1.4 {_FONT};letter-spacing:.12em;text-transform:uppercase;'
                f'color:#0B1526;">{_e(title)}</div>'
                '<div style="height:2px;width:28px;background:#EE5A4E;margin:6px 0 10px;font-size:0;'
                'line-height:0;">&nbsp;</div>'
                f"{body}</td></tr>")

    risk_rows = []
    for flag in analysis.red_flags[:6]:
        background, ink = _SEVERITY_STYLE[flag.severity]
        risk_rows.append(
            "<tr>"
            f'<td style="padding:9px 10px 9px 0;vertical-align:top;width:84px;border-bottom:1px solid #EEF2F6;">'
            f'<span style="display:inline-block;padding:3px 8px;border-radius:99px;background:{background};'
            f'color:{ink};font:700 9px/1.4 {_FONT};letter-spacing:.06em;">{_e(flag.severity.value)}</span></td>'
            f'<td style="padding:9px 0;vertical-align:top;border-bottom:1px solid #EEF2F6;'
            f'font:400 13px/1.5 {_FONT};color:#1F2733;"><b>{_e(flag.issue)}</b>'
            f'<div style="font-size:11.5px;color:#5B6573;margin-top:2px;">{_e(flag.classification.value)} · '
            f"{_e(shorten(flag.evidence, 180))}</div>"
            f'<div style="font-size:12px;margin-top:4px;">{_e(shorten(flag.business_consequence, 220))}</div>'
            "</td></tr>")
    extra = len(analysis.red_flags) - len(risk_rows)
    risks = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0">' + "".join(risk_rows)
             + "</table>" if risk_rows
             else f'<div style="font:400 13px/1.5 {_FONT};color:#5B6573;">No red flags identified.</div>')
    if extra > 0:
        risks += (f'<div style="font:400 11.5px/1.5 {_FONT};color:#7E90A8;margin-top:6px;">'
                  f"+{extra} more in the attached analysis JSON.</div>")

    questions = (
        f'<ol style="margin:0;padding-left:18px;font:400 13px/1.55 {_FONT};color:#1F2733;">'
        + "".join(f'<li style="margin:0 0 6px;">{_e(q.question)} '
                  f'<span style="font-size:11px;color:#7E90A8;">[{_e(q.audience)}]</span></li>'
                  for q in analysis.diligence_questions[:5])
        + "</ol>"
    )

    def facts(rows: list[tuple[str, str]]) -> str:
        return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
                + "".join(f'<tr><td style="padding:3px 16px 3px 0;width:150px;vertical-align:top;'
                          f'font:400 12px/1.5 {_FONT};color:#7E90A8;">{_e(label)}</td>'
                          f'<td style="padding:3px 0;font:600 12px/1.5 {_FONT};color:#1F2733;">{_e(value)}</td></tr>'
                          for label, value in rows)
                + "</table>")

    warnings = ""
    if analysis.warnings:
        warnings = section("Warnings", f'<ul style="margin:0;padding-left:18px;font:400 12px/1.5 {_FONT};'
                                       f'color:#8A5314;">'
                           + "".join(f"<li>{_e(w)}</li>" for w in analysis.warnings[:5]) + "</ul>")

    today = analysis.generated_at
    return f"""\
<!doctype html>
<html><body style="margin:0;padding:24px 12px;background:#F3F6FA;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;margin:0 auto;background:#FFFFFF;border:1px solid #E3E8EF;border-radius:10px;overflow:hidden;">
  <tr><td style="background:#0B1526;padding:22px 26px;">
    <div style="font:700 10px/1.4 {_FONT};letter-spacing:.14em;text-transform:uppercase;color:#35BEBB;">TEN Capital Network · Tech &amp; IP Diligence Review</div>
    <div style="font:700 20px/1.3 {_FONT};color:#FFFFFF;margin-top:6px;">{_e(snap.display_name())}</div>
    <div style="font:400 12px/1.5 {_FONT};color:#C4D0E0;margin-top:5px;">{_e(meta)}</div>
  </td></tr>
  <tr><td style="height:3px;background:#EE5A4E;background-image:linear-gradient(90deg,#EE5A4E,#F3A22A,#35BEBB);font-size:0;line-height:0;">&nbsp;</td></tr>
  <tr><td style="padding:22px 26px 0;">{metrics}{conclusion_box}</td></tr>
  {section("Top risks", risks)}
  {section("Priority diligence questions", questions)}
  {section("Company snapshot", facts(_snapshot_rows(analysis)))}
  {section("Run details", facts(_run_rows(analysis, record)))}
  {warnings}
  <tr><td style="padding:22px 26px 24px;">
    <div style="border-top:1px solid #E3E8EF;padding-top:14px;font:400 11px/1.6 {_FONT};color:#7E90A8;">
      Attached: the one-page PDF and the structured analysis JSON{"; the TEN Capital lead record" if _consented(record) else ""}.<br>
      Deck-based screening only; not legal advice, a patent-validity or freedom-to-operate opinion, or a security audit.<br>
      Compiled on {today:%B} {today.day}, {today.year} by TEN Capital Network
    </div>
  </td></tr>
</table>
</body></html>"""


def build_attachments(pdf_path: Path, analysis_path: Path, record_path: Path,
                      record: TenCapitalRecord) -> list[dict[str, str]]:
    files = [pdf_path, analysis_path] + ([record_path] if _consented(record) else [])
    return [{"filename": path.name, "content": base64.b64encode(path.read_bytes()).decode("ascii")} for path in files]


def send_report_email(analysis: DiligenceAnalysis, record: TenCapitalRecord, *, pdf_path: Path,
                      analysis_path: Path, record_path: Path, settings: Settings,
                      transport: httpx.BaseTransport | None = None, subject_prefix: str = "") -> EmailResult:
    """Email the finished review to the configured TEN Capital recipients. Never raises."""
    if not settings.email_reports:
        return EmailResult(status=EmailStatus.DISABLED, detail="Result emails are disabled (TDIP_EMAIL_REPORTS).")
    key = settings.resend_api_key.get_secret_value().strip() if settings.resend_api_key else ""
    to = recipients(settings)
    if not key or not to:
        return EmailResult(status=EmailStatus.NOT_CONFIGURED,
                           detail="Result emails not configured (set RESEND_API_KEY and TDIP_REPORT_EMAIL_TO).")
    try:
        payload: dict[str, object] = {
            "from": settings.report_email_from,
            "to": to,
            "subject": subject_prefix + build_subject(analysis),
            "html": build_html(analysis, record),
            "text": build_text(analysis, record),
            "attachments": build_attachments(Path(pdf_path), Path(analysis_path), Path(record_path), record),
        }
        if _consented(record) and _EMAIL_ADDRESS.match(record.user):
            payload["reply_to"] = [record.user]
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                   "Idempotency-Key": f"tdip-{record.record_id}",
                   "User-Agent": "ten-capital-tech-ip-diligence/1.0"}
        with httpx.Client(timeout=settings.resend_timeout_seconds, transport=transport) as client:
            response = client.post(RESEND_ENDPOINT, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        log.error("Result email could not be sent: %s", exc)
        return EmailResult(status=EmailStatus.FAILED, detail=f"Network error sending email ({type(exc).__name__}).",
                           recipients=to)
    except Exception as exc:  # building the message must never break the run
        log.exception("Result email could not be prepared")
        return EmailResult(status=EmailStatus.FAILED, detail=f"Email could not be prepared ({type(exc).__name__}).",
                           recipients=to)

    if 200 <= response.status_code < 300:
        try:
            message_id = response.json().get("id")
        except ValueError:
            message_id = None
        log.info("Result email sent to %s (id=%s)", ", ".join(to), message_id)
        return EmailResult(status=EmailStatus.SENT, detail=f"Emailed to {', '.join(to)}.", message_id=message_id,
                           recipients=to)
    try:
        reason = str(response.json().get("message") or "")
    except ValueError:
        reason = response.text
    log.error("Resend rejected the result email (HTTP %s): %s", response.status_code, reason)
    return EmailResult(status=EmailStatus.FAILED,
                       detail=f"Resend returned HTTP {response.status_code}: {shorten(reason, 160)}", recipients=to)
