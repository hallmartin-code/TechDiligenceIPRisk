"""Web upload flow and CLI entry point."""

from __future__ import annotations

import re
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import EXIT_OK, main
from app.web.server import create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings(engine="rules", output_dir=tmp_path, ten_capital_endpoint=None, ten_capital_api_key=None)
    return TestClient(create_app(settings))


def wait_for(client: TestClient, status_url: str, timeout: float = 90) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(status_url).json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_index_page_uses_ten_capital_design(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert "Powered by TEN Capital Network" in html
    assert 'accept=".pdf,.pptx,.ppt"' in html
    assert "Check your downloads for the result." in html
    assert "{{" not in html  # every placeholder filled
    assert "rules engine" in html and "Anthropic" not in html  # engine wording matches the configured engine
    consent_input = re.search(r'<input[^>]*id="consent"[^>]*>', html).group(0)
    assert "checked" not in consent_input  # consent is never pre-selected
    assert client.get("/assets/logo.png").status_code == 200


def test_upload_without_consent_keeps_record_local(client, risky_pdf):
    files = {"deck": ("quickmind.pdf", risky_pdf.read_bytes(), "application/pdf")}
    response = client.post("/api/jobs", files=files, data={"user": "analyst@example.com"})
    assert response.status_code == 202
    status = wait_for(client, response.json()["status_url"])
    assert status["status"] == "done", status
    assert status["result"]["consent_status"] == "NOT_GRANTED"
    assert status["result"]["critical_flags"] >= 3 and "high_flags" in status["result"]

    pdf = client.get(status["downloads"]["pdf"])
    assert pdf.content[:4] == b"%PDF" and "attachment" in pdf.headers["content-disposition"]
    record = client.get(status["downloads"]["record"]).json()
    assert record["consent_status"] == "NOT_GRANTED" and record["transmission_status"] == "NOT_SENT_NO_CONSENT"
    assert record["user"] == "analyst@example.com"
    assert client.get(status["downloads"]["analysis"]).json()["company"]["name"]["value"] == "QuickMind AI"


@pytest.mark.parametrize("value,expected", [("granted", "GRANTED"), ("true", "NOT_GRANTED"), ("on", "NOT_GRANTED")])
def test_consent_checkbox_value(client, strong_pptx, value, expected):
    files = {"deck": ("helix.pptx", strong_pptx.read_bytes(),
                      "application/vnd.openxmlformats-officedocument.presentationml.presentation")}
    response = client.post("/api/jobs", files=files, data={"consent": value})
    status = wait_for(client, response.json()["status_url"])
    record = client.get(status["downloads"]["record"]).json()
    assert record["consent_status"] == expected


def test_rejects_unsupported_upload(client):
    response = client.post("/api/jobs", files={"deck": ("notes.txt", b"hello", "text/plain")})
    assert response.status_code == 400


def test_malformed_upload_reports_readable_error(client):
    response = client.post("/api/jobs", files={"deck": ("broken.pdf", b"definitely not a pdf", "application/pdf")})
    status = wait_for(client, response.json()["status_url"])
    assert status["status"] == "error" and "PDF" in status["error"]


def test_password_gate(tmp_path):
    settings = Settings(engine="rules", output_dir=tmp_path, app_password="s3cret-pass")
    gated = TestClient(create_app(settings))
    assert gated.get("/").status_code == 401
    assert "Basic" in gated.get("/").headers["www-authenticate"]
    assert gated.get("/healthz").status_code == 200  # platform health checks stay open
    assert gated.get("/", auth=("tencapital", "wrong")).status_code == 401
    assert gated.post("/api/jobs", files={"deck": ("a.pdf", b"%PDF", "application/pdf")}).status_code == 401
    assert gated.get("/", auth=("anyone", "s3cret-pass")).status_code == 200


def test_favicon_and_public_assets(client):
    html = client.get("/").text
    for href in ('href="/favicon.ico"', 'href="/public/favicon-32x32.png"', 'href="/public/favicon-16x16.png"',
                 'href="/public/apple-touch-icon.png"', 'href="/public/site.webmanifest"'):
        assert href in html
    ico = client.get("/favicon.ico")
    assert ico.status_code == 200 and ico.headers["content-type"] == "image/x-icon"
    for path in ("/public/favicon-16x16.png", "/public/favicon-32x32.png", "/public/apple-touch-icon.png",
                 "/public/icon-192.png", "/public/icon-512.png", "/public/icon-512-maskable.png"):
        response = client.get(path)
        assert response.status_code == 200 and response.content[:8] == b"\x89PNG\r\n\x1a\n", path
    manifest = client.get("/public/site.webmanifest").json()
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200


def test_icons_load_without_password(tmp_path):
    gated = TestClient(create_app(Settings(engine="rules", output_dir=tmp_path, app_password="s3cret-pass")))
    assert gated.get("/favicon.ico").status_code == 200
    assert gated.get("/public/favicon-32x32.png").status_code == 200
    assert gated.get("/").status_code == 401  # the app itself stays protected


def test_unknown_job(client):
    assert client.get("/api/jobs/does-not-exist").status_code == 404


def test_cli_writes_pdf_json_and_record(strong_pptx, tmp_path, capsys):
    code = main(["analyze", str(strong_pptx), "--engine", "rules", "--output-dir", str(tmp_path),
                 "--user", "analyst@example.com"])
    assert code == EXIT_OK
    assert len(list(tmp_path.glob("*.pdf"))) == 1
    assert len(list(tmp_path.glob("*_record.json"))) == 1
    assert len(list(tmp_path.glob("*.json"))) == 2
    out = capsys.readouterr().out
    assert "Helix Grid Systems" in out and "PDF:" in out
