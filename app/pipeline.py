"""End-to-end run: ingest -> analyze -> validate -> one-page PDF -> JSON -> TEN Capital record.

Used identically by the CLI and the web server, so both produce the same files
from the same deck.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.analysis.engine import analyze_deck
from app.config import Settings, get_settings
from app.ingestion.extractor import load_deck
from app.integrations.ten_capital import (
    ConsentStatus,
    SubmissionResult,
    TenCapitalRecord,
    build_record,
    submit_record,
    write_record,
)
from app.models.schemas import DiligenceAnalysis
from app.reporting.pdf_generator import generate_pdf
from app.utils.logging import get_logger
from app.utils.text import slugify
from app.utils.validation import validate_analysis

log = get_logger("pipeline")

STEPS = ("Reading deck", "Analyzing technology and IP", "Validating analysis", "Generating one-page PDF",
         "Preparing TEN Capital record")


@dataclass
class DiligenceRun:
    analysis: DiligenceAnalysis
    pdf_path: Path
    analysis_path: Path
    record_path: Path
    record: TenCapitalRecord
    submission: SubmissionResult
    validation_warnings: list[str] = field(default_factory=list)


def run_diligence(
    deck_path: str | Path,
    *,
    settings: Settings | None = None,
    user: str | None = None,
    consent: ConsentStatus = ConsentStatus.NOT_GRANTED,
    output_dir: str | Path | None = None,
    engine: str | None = None,
    client: Any | None = None,
    transport: httpx.BaseTransport | None = None,
    progress: Callable[[int, str], None] | None = None,
) -> DiligenceRun:
    settings = settings or get_settings()
    deck_path = Path(deck_path)

    def step(index: int) -> None:
        log.info("[%d/%d] %s", index + 1, len(STEPS), STEPS[index])
        if progress:
            progress(index + 1, STEPS[index])

    step(0)
    deck = load_deck(deck_path, settings)
    step(1)
    analysis = analyze_deck(deck, settings, client=client, engine=engine,
                            source_bytes=deck_path.read_bytes() if deck.file_type == "pdf" else None)
    step(2)
    warnings = validate_analysis(analysis)

    out = Path(output_dir) if output_dir else Path(settings.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{slugify(analysis.company.display_name(), 50)}_tech_ip_review_{analysis.generated_at:%Y%m%d_%H%M%S}"

    step(3)
    pdf_path = generate_pdf(analysis, out / f"{stem}.pdf", settings)
    analysis_path = out / f"{stem}.json"
    analysis_path.write_text(analysis.model_dump_json(indent=2), encoding="utf-8")

    step(4)
    record = build_record(analysis, user=user or settings.default_user, consent=consent)
    submission = submit_record(record, settings, transport)
    record = record.model_copy(update={"transmission_status": submission.status})
    record_path = write_record(record, out / f"{stem}_ten_capital_record.json")

    return DiligenceRun(analysis=analysis, pdf_path=pdf_path, analysis_path=analysis_path, record_path=record_path,
                        record=record, submission=submission, validation_warnings=warnings)
