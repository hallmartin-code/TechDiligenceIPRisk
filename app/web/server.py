"""Web upload interface (FastAPI).

    python -m app.main serve            # http://127.0.0.1:8040
    uvicorn app.web.server:app --host 0.0.0.0 --port 8040 --workers 1

A run can take minutes with the LLM engine, so an upload starts a background
job and the page polls for progress, then downloads the PDF automatically.
Jobs live in process memory: run a single worker process.

Consent is read from a checkbox that is unchecked by default; only the exact
submitted value "granted" counts as consent.
"""

from __future__ import annotations

import base64
import hmac
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import httpx

from app.analysis.engine import AnalysisError
from app.config import LOGO_PATH, Settings, get_settings
from app.ingestion.errors import DeckIngestionError
from app.models.schemas import Severity
from app.integrations.ten_capital import resolve_consent
from app.pipeline import STEPS, DiligenceRun, run_diligence
from app.reporting.pdf_generator import OnePageError
from app.utils.logging import configure_logging, get_logger
from app.utils.validation import AnalysisValidationError

log = get_logger("web")

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "index.html"
PUBLIC_DIR = Path(__file__).resolve().parent / "public"
ALLOWED_SUFFIXES = {".pdf", ".ppt", ".pptx"}

# Served without the password: platform health checks and non-sensitive brand assets
# (browsers request favicons before, and independently of, the Basic-auth prompt).
OPEN_PATHS = {"/healthz", "/favicon.ico"}
OPEN_PREFIXES = ("/public/",)


@dataclass
class Job:
    id: str
    directory: Path
    filename: str
    created: float = field(default_factory=time.time)
    status: str = "queued"
    step: int = 0
    step_label: str = "Queued"
    error: str | None = None
    run: DiligenceRun | None = None


def _safe_filename(name: str) -> str:
    base = Path(name or "deck").name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return (cleaned or "deck")[-120:]


def create_app(settings: Settings | None = None, transport: httpx.BaseTransport | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_file)
    app = FastAPI(title="Technical & IP Diligence Review", docs_url=None, redoc_url=None, openapi_url=None)

    password = settings.app_password.get_secret_value() if settings.app_password else None
    if password is None:
        log.warning("TDIP_APP_PASSWORD is not set; the web interface is open to anyone who can reach it")

    @app.middleware("http")
    async def require_password(request: Request, call_next):
        """HTTP Basic authentication (any username) when TDIP_APP_PASSWORD is set. Health and icons stay open."""
        path = request.url.path
        if password is None or path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        supplied = ""
        if header.lower().startswith("basic "):
            try:
                supplied = base64.b64decode(header[6:]).decode("utf-8").partition(":")[2]
            except (ValueError, UnicodeDecodeError):
                supplied = ""
        if not hmac.compare_digest(supplied.encode("utf-8"), password.encode("utf-8")):
            return Response("Authentication required.", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="TEN Capital Tech & IP Diligence", charset="UTF-8"'})
        return await call_next(request)

    jobs: dict[str, Job] = {}
    lock = threading.Lock()
    slots = threading.BoundedSemaphore(settings.web_max_concurrent_jobs)
    workdir = Path(tempfile.mkdtemp(prefix="tdip_web_"))
    max_bytes = int(settings.max_upload_mb * 1024 * 1024)

    def purge_expired() -> None:
        cutoff = time.time() - settings.web_job_ttl_minutes * 60
        with lock:
            expired = [j for j in jobs.values() if j.created < cutoff and j.status in ("done", "error")]
            for job in expired:
                jobs.pop(job.id, None)
                shutil.rmtree(job.directory, ignore_errors=True)

    def execute(job: Job, deck_path: Path, user: str | None, consent) -> None:
        def progress(step: int, label: str) -> None:
            job.step, job.step_label = step, label

        job.status = "running"
        try:
            job.run = run_diligence(deck_path, settings=settings, user=user, consent=consent,
                                    output_dir=job.directory / "out", transport=transport, progress=progress)
            job.status, job.step_label = "done", "Complete"
        except DeckIngestionError as exc:
            job.status, job.error = "error", str(exc)
        except (AnalysisError, AnalysisValidationError, OnePageError) as exc:
            job.status, job.error = "error", f"The analysis could not be completed: {exc}"
        except Exception:
            log.exception("Job %s failed", job.id)
            job.status, job.error = "error", "Unexpected error while analyzing the deck. See server logs."
        finally:
            slots.release()

    def get_job(job_id: str) -> Job:
        with lock:
            job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown or expired job.")
        return job

    uses_claude = settings.engine == "llm" or (settings.engine == "auto" and settings.llm_credentials_available())
    today = date.today()
    page_values = {
        "{{DATE}}": f"{today:%B} {today.day}, {today.year}",
        "{{MAX_MB}}": f"{settings.max_upload_mb:g}",
        "{{ENGINE_LABEL}}": "Claude" if uses_claude else "TEN Capital's deterministic rules engine",
        "{{DURATION_NOTE}}": ("Claude analysis usually takes 3–4 minutes." if uses_claude
                              else "Analysis usually takes under a minute."),
        "{{DATA_NOTE}}": ("Your deck is processed on this server and its contents are sent to Anthropic's Claude API "
                          "for analysis." if uses_claude
                          else "Your deck is processed on this server; no external AI service is used."),
    }

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        for placeholder, value in page_values.items():
            html = html.replace(placeholder, value)
        return html

    app.mount("/public", StaticFiles(directory=PUBLIC_DIR), name="public")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(PUBLIC_DIR / "favicon.ico", media_type="image/x-icon",
                            headers={"Cache-Control": "public, max-age=604800"})

    @app.get("/assets/logo.png")
    def logo() -> FileResponse:
        return FileResponse(LOGO_PATH, media_type="image/png")

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/jobs")
    async def create_job(deck: UploadFile = File(...), user: str = Form(""), consent: str = Form("")) -> JSONResponse:
        filename = _safe_filename(deck.filename or "")
        if Path(filename).suffix.lower() not in ALLOWED_SUFFIXES:
            raise HTTPException(status_code=400, detail="Upload a PDF, PPT or PPTX pitch deck.")
        data = await deck.read(max_bytes + 1)
        if not data:
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")
        if len(data) > max_bytes:
            raise HTTPException(status_code=413, detail=f"The file exceeds {settings.max_upload_mb:g} MB.")

        purge_expired()
        if not slots.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="The analyzer is busy. Try again in a minute.")
        job = Job(id=uuid.uuid4().hex, directory=workdir / uuid.uuid4().hex, filename=filename)
        try:
            job.directory.mkdir(parents=True)
            deck_path = job.directory / filename
            deck_path.write_bytes(data)
            consent_status = resolve_consent(consent == "granted")
            with lock:
                jobs[job.id] = job
            threading.Thread(target=execute, args=(job, deck_path, user.strip()[:200] or None, consent_status),
                             daemon=True).start()
        except Exception:
            slots.release()
            raise
        return JSONResponse({"job_id": job.id, "status_url": f"/api/jobs/{job.id}"}, status_code=202)

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict:
        job = get_job(job_id)
        body: dict = {"job_id": job.id, "status": job.status, "step": job.step, "total_steps": len(STEPS),
                      "step_label": job.step_label, "error": job.error}
        if job.run is not None:
            analysis = job.run.analysis
            conclusion = analysis.investor_conclusion
            body["result"] = {
                "company": analysis.company.display_name(),
                "overall_score": analysis.scores.overall_score,
                "uncertainty": analysis.scores.uncertainty,
                "evidence_confidence": analysis.evidence_confidence,
                "technical_position": conclusion.technical_position.value,
                "ip_position": conclusion.ip_position.value,
                "transaction_readiness": conclusion.transaction_readiness.value,
                "engine": analysis.engine.name,
                "critical_flags": len(analysis.flags_at(Severity.CRITICAL)),
                "high_flags": len(analysis.flags_at(Severity.HIGH)),
                "consent_status": job.run.record.consent_status.value,
                "transmission": job.run.submission.detail,
            }
            body["downloads"] = {"pdf": f"/api/jobs/{job.id}/report.pdf",
                                 "analysis": f"/api/jobs/{job.id}/analysis.json",
                                 "record": f"/api/jobs/{job.id}/record.json"}
        return body

    def finished_file(job_id: str, attribute: str, media_type: str) -> FileResponse:
        job = get_job(job_id)
        if job.run is None:
            raise HTTPException(status_code=409, detail="The analysis is not finished.")
        path: Path = getattr(job.run, attribute)
        return FileResponse(path, media_type=media_type, filename=path.name)

    @app.get("/api/jobs/{job_id}/report.pdf")
    def report(job_id: str) -> FileResponse:
        return finished_file(job_id, "pdf_path", "application/pdf")

    @app.get("/api/jobs/{job_id}/analysis.json")
    def analysis_json(job_id: str) -> FileResponse:
        return finished_file(job_id, "analysis_path", "application/json")

    @app.get("/api/jobs/{job_id}/record.json")
    def record_json(job_id: str) -> FileResponse:
        return finished_file(job_id, "record_path", "application/json")

    return app


app = create_app()
