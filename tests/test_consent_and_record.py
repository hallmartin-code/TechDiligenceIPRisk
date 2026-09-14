"""Consent logic and the structured TEN Capital lead-intelligence record."""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from app.config import Settings, get_settings
from app.integrations.ten_capital import (
    ConsentStatus,
    TenCapitalRecord,
    TransmissionStatus,
    build_record,
    resolve_consent,
    submit_record,
)
from app.main import main
from app.pipeline import run_diligence

SPEC_FIELDS = {
    "user", "company", "sector", "stage", "raise_amount", "revenue", "growth", "gross_margin",
    "overall_technical_ip_score", "evidence_confidence", "technical_architecture_rating",
    "development_maturity_rating", "scalability_rating", "engineering_organization_rating", "cybersecurity_rating",
    "ip_ownership_rating", "ip_defensibility_rating", "third_party_dependency_rating",
    "transaction_readiness_rating", "critical_red_flags", "high_severity_red_flags", "top_diligence_questions",
    "major_missing_information", "summary_findings", "recommended_diligence_areas", "consent_status",
    "report_created_at",
}


def _forbidden_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"network call attempted without consent: {request.url}")
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("value", [False, None, "true", "True", "yes", "granted", "GRANTED", 1, "1", object()])
def test_only_literal_true_grants_consent(value):
    assert resolve_consent(value) is ConsentStatus.NOT_GRANTED


def test_literal_true_grants_consent():
    assert resolve_consent(True) is ConsentStatus.GRANTED


def test_build_record_rejects_inferred_consent(risky):
    _, analysis = risky
    with pytest.raises(TypeError):
        build_record(analysis, user="a@b.com", consent=True)  # type: ignore[arg-type]


def test_record_contains_every_required_field(risky):
    _, analysis = risky
    record = build_record(analysis, user="analyst@example.com", consent=ConsentStatus.NOT_GRANTED)
    payload = json.loads(record.model_dump_json())
    assert SPEC_FIELDS <= set(payload)
    assert payload["consent_status"] == "NOT_GRANTED"
    assert payload["transmission_status"] == "NOT_SENT_NO_CONSENT"
    assert payload["company"] == "QuickMind AI"
    assert payload["critical_red_flags"] and len(payload["top_diligence_questions"]) == 5
    assert payload["recommended_diligence_areas"]
    assert payload["gross_margin"] == "NOT PROVIDED"
    for key in [k for k in payload if k.endswith("_rating")]:
        assert 1 <= payload[key] <= 5
    datetime.fromisoformat(payload["report_created_at"])
    assert TenCapitalRecord.model_validate(payload).record_id == record.record_id


def test_missing_user_is_labeled_not_provided(strong):
    _, analysis = strong
    assert build_record(analysis, user=None, consent=ConsentStatus.NOT_GRANTED).user == "NOT PROVIDED"


def test_no_transmission_without_consent(risky):
    _, analysis = risky
    settings = Settings(engine="rules", ten_capital_endpoint="https://intake.example.com/records")
    record = build_record(analysis, user="a@b.com", consent=ConsentStatus.NOT_GRANTED)
    result = submit_record(record, settings, transport=_forbidden_transport())
    assert result.status == TransmissionStatus.NOT_SENT_NO_CONSENT


def test_transmission_with_consent(risky):
    _, analysis = risky
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["headers"] = request.headers
        return httpx.Response(201, json={"ok": True})

    settings = Settings(engine="rules", ten_capital_endpoint="https://intake.example.com/records",
                        ten_capital_api_key="secret-token")
    record = build_record(analysis, user="a@b.com", consent=ConsentStatus.GRANTED)
    result = submit_record(record, settings, transport=httpx.MockTransport(handler))
    assert result.status == TransmissionStatus.SENT and result.http_status == 201
    assert captured["body"]["consent_status"] == "GRANTED"
    assert captured["body"]["company"] == "QuickMind AI"
    assert "transmission_status" not in captured["body"]
    assert captured["headers"]["authorization"] == "Bearer secret-token"
    assert captured["headers"]["idempotency-key"] == record.record_id


def test_consent_without_endpoint_is_not_configured(risky):
    _, analysis = risky
    record = build_record(analysis, user=None, consent=ConsentStatus.GRANTED)
    result = submit_record(record, Settings(engine="rules", ten_capital_endpoint=None))
    assert result.status == TransmissionStatus.NOT_CONFIGURED


def test_insecure_endpoint_refused(risky):
    _, analysis = risky
    record = build_record(analysis, user=None, consent=ConsentStatus.GRANTED)
    result = submit_record(record, Settings(engine="rules", ten_capital_endpoint="http://intake.example.com"),
                           transport=_forbidden_transport())
    assert result.status == TransmissionStatus.FAILED and "HTTPS" in result.detail


def test_server_error_reported_as_failed(risky):
    _, analysis = risky
    record = build_record(analysis, user=None, consent=ConsentStatus.GRANTED)
    settings = Settings(engine="rules", ten_capital_endpoint="https://intake.example.com/records")
    result = submit_record(record, settings, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert result.status == TransmissionStatus.FAILED and result.http_status == 500


def test_pipeline_defaults_to_local_only(risky_pdf, tmp_path, monkeypatch):
    # Environment variables must never be able to grant consent.
    monkeypatch.setenv("TDIP_CONSENT", "GRANTED")
    monkeypatch.setenv("CONSENT", "true")
    settings = Settings(engine="rules", output_dir=tmp_path, ten_capital_endpoint="https://intake.example.com/r")
    run = run_diligence(risky_pdf, settings=settings, user="a@b.com", transport=_forbidden_transport())
    saved = json.loads(run.record_path.read_text(encoding="utf-8"))
    assert saved["consent_status"] == "NOT_GRANTED"
    assert saved["transmission_status"] == "NOT_SENT_NO_CONSENT"
    assert run.pdf_path.exists() and run.analysis_path.exists()


def test_pipeline_sends_with_explicit_consent(strong_pptx, tmp_path):
    calls = []
    settings = Settings(engine="rules", output_dir=tmp_path, ten_capital_endpoint="https://intake.example.com/r")
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200))
    run = run_diligence(strong_pptx, settings=settings, consent=ConsentStatus.GRANTED, transport=transport)
    assert len(calls) == 1
    assert json.loads(run.record_path.read_text(encoding="utf-8"))["transmission_status"] == "SENT"


def test_cli_consent_flag(strong_pptx, tmp_path, monkeypatch):
    monkeypatch.delenv("TDIP_TEN_CAPITAL_ENDPOINT", raising=False)
    get_settings.cache_clear()
    try:
        base = ["analyze", str(strong_pptx), "--engine", "rules", "--quiet"]
        assert main(base + ["--output-dir", str(tmp_path / "no")]) == 0
        assert main(base + ["--output-dir", str(tmp_path / "yes"), "--consent-share-with-ten-capital"]) == 0
    finally:
        get_settings.cache_clear()
    without = json.loads(next((tmp_path / "no").glob("*_record.json")).read_text(encoding="utf-8"))
    granted = json.loads(next((tmp_path / "yes").glob("*_record.json")).read_text(encoding="utf-8"))
    assert without["consent_status"] == "NOT_GRANTED"
    assert granted["consent_status"] == "GRANTED" and granted["transmission_status"] == "NOT_CONFIGURED"
