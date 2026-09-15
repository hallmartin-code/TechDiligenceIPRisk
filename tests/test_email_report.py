"""Automated result emails via Resend (all network calls mocked)."""

from __future__ import annotations

import base64
import html
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.integrations.email_report import (
    RESEND_ENDPOINT,
    EmailStatus,
    build_html,
    build_subject,
    send_report_email,
)
from app.integrations.ten_capital import ConsentStatus, build_record
from app.models.schemas import Classification, SnapshotField
from app.pipeline import run_diligence
from app.reporting.pdf_generator import generate_pdf
from app.web.server import create_app

UPLOADER = "founder@startup.example"


def _forbidden() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network call: {request.url}")
    return httpx.MockTransport(handler)


def _capture(status: int = 200, body: dict | None = None):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json=body if body is not None else {"id": "email_123"})
    return calls, httpx.MockTransport(handler)


def _email_settings(**overrides) -> Settings:
    values = {"engine": "rules", "resend_api_key": "re_test_key", "ten_capital_endpoint": None}
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def outputs(risky, tmp_path):
    _, analysis = risky

    def make(consent: ConsentStatus):
        record = build_record(analysis, user=UPLOADER, consent=consent)
        pdf = generate_pdf(analysis, tmp_path / "review.pdf", Settings(engine="rules"))
        analysis_path = tmp_path / "review.json"
        analysis_path.write_text(analysis.model_dump_json(), encoding="utf-8")
        record_path = tmp_path / "review_ten_capital_record.json"
        record_path.write_text(record.model_dump_json(), encoding="utf-8")
        return analysis, record, {"pdf_path": pdf, "analysis_path": analysis_path, "record_path": record_path}

    return make


def test_not_configured_without_key(outputs):
    analysis, record, files = outputs(ConsentStatus.NOT_GRANTED)
    result = send_report_email(analysis, record, **files, settings=_email_settings(resend_api_key=None),
                               transport=_forbidden())
    assert result.status == EmailStatus.NOT_CONFIGURED


def test_disabled_by_setting(outputs):
    analysis, record, files = outputs(ConsentStatus.NOT_GRANTED)
    result = send_report_email(analysis, record, **files, settings=_email_settings(email_reports=False),
                               transport=_forbidden())
    assert result.status == EmailStatus.DISABLED


def test_every_analysis_is_emailed_without_uploader_identity(outputs):
    analysis, record, files = outputs(ConsentStatus.NOT_GRANTED)
    calls, transport = _capture()
    result = send_report_email(analysis, record, **files, settings=_email_settings(), transport=transport)

    assert result.status == EmailStatus.SENT and result.message_id == "email_123"
    request = calls[0]
    assert str(request.url) == RESEND_ENDPOINT
    assert request.headers["authorization"] == "Bearer re_test_key"
    assert record.record_id in request.headers["idempotency-key"]

    body = json.loads(request.content)
    assert body["to"] == ["Info@tencapital.group"]
    assert body["from"].endswith("<reports@tencapital.group>")
    assert "QuickMind AI" in body["subject"] and "critical" in body["subject"]
    assert [a["filename"] for a in body["attachments"]] == ["review.pdf", "review.json"]  # no lead record
    assert base64.b64decode(body["attachments"][0]["content"])[:4] == b"%PDF"
    assert UPLOADER not in body["html"] and UPLOADER not in body["text"]
    assert "reply_to" not in body
    assert "consent not granted" in body["html"].lower()
    assert html.escape(analysis.red_flags[0].issue) in body["html"]
    assert html.escape(analysis.diligence_questions[0].question) in body["html"]


def test_consent_adds_contact_details_and_lead_record(outputs):
    analysis, record, files = outputs(ConsentStatus.GRANTED)
    calls, transport = _capture()
    send_report_email(analysis, record, **files, settings=_email_settings(), transport=transport)
    body = json.loads(calls[0].content)
    assert [a["filename"] for a in body["attachments"]] == ["review.pdf", "review.json",
                                                             "review_ten_capital_record.json"]
    assert body["reply_to"] == [UPLOADER]
    assert UPLOADER in body["html"] and UPLOADER in body["text"]


def test_multiple_recipients(outputs):
    analysis, record, files = outputs(ConsentStatus.NOT_GRANTED)
    calls, transport = _capture()
    settings = _email_settings(report_email_to="Info@tencapital.group, partners@tencapital.group")
    send_report_email(analysis, record, **files, settings=settings, transport=transport)
    assert json.loads(calls[0].content)["to"] == ["Info@tencapital.group", "partners@tencapital.group"]


def test_deck_content_is_escaped_in_html(risky):
    _, analysis = risky
    variant = analysis.model_copy(deep=True)
    variant.company.name = SnapshotField(value="<script>alert(1)</script> Corp", classification=Classification.FACT,
                                         slides=[1], excerpt="x")
    record = build_record(variant, user=None, consent=ConsentStatus.NOT_GRANTED)
    body = build_html(variant, record)
    assert "<script>" not in body and "&lt;script&gt;" in body
    assert "Tech & IP Review" in build_subject(variant)


def test_resend_rejection_is_reported(outputs):
    analysis, record, files = outputs(ConsentStatus.NOT_GRANTED)
    _, transport = _capture(422, {"message": "The from address is not verified."})
    result = send_report_email(analysis, record, **files, settings=_email_settings(), transport=transport)
    assert result.status == EmailStatus.FAILED and "422" in result.detail and "not verified" in result.detail


def test_pipeline_emails_each_run_and_survives_email_failure(risky_pdf, tmp_path):
    settings = _email_settings(output_dir=tmp_path)
    calls, transport = _capture()
    run = run_diligence(risky_pdf, settings=settings, email_transport=transport)
    assert run.email.status == EmailStatus.SENT and len(calls) == 1

    _, failing = _capture(500, {"message": "Service unavailable"})
    failed = run_diligence(risky_pdf, settings=settings, email_transport=failing)
    assert failed.email.status == EmailStatus.FAILED
    assert failed.pdf_path.exists() and failed.analysis_path.exists()  # the analysis is never lost


def test_pipeline_can_skip_email(risky_pdf, tmp_path):
    run = run_diligence(risky_pdf, settings=_email_settings(output_dir=tmp_path), send_email=False,
                        email_transport=_forbidden())
    assert run.email.status == EmailStatus.DISABLED


def test_upload_page_discloses_emailing(tmp_path):
    with_email = TestClient(create_app(_email_settings(output_dir=tmp_path))).get("/").text
    assert "emailed to the TEN Capital team" in with_email
    assert "Include my contact details." in with_email
    without_email = TestClient(create_app(Settings(engine="rules", output_dir=tmp_path))).get("/").text
    assert "Nothing is shared with TEN Capital unless you tick the box above." in without_email
    assert "{{" not in with_email and "{{" not in without_email
