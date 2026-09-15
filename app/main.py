"""Command-line entry point.

    python -m app.main analyze path/to/deck.pdf
    python -m app.main analyze deck.pptx --engine rules --output-dir output --user analyst@fund.com
    python -m app.main analyze deck.pdf --consent-share-with-ten-capital
    python -m app.main analyze deck.pdf --no-email      # skip the automated result email
    python -m app.main serve --port 8040

Exit codes: 0 success, 2 deck could not be read, 3 analysis failed, 4 report could not be produced.
"""

from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.utils.logging import configure_logging

EXIT_OK, EXIT_INGESTION, EXIT_ANALYSIS, EXIT_REPORT = 0, 2, 3, 4


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tdip", description="Technical & IP Diligence Review for pitch decks.")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="Analyze a PDF, PPT or PPTX pitch deck.")
    analyze.add_argument("deck", help="Path to the pitch deck.")
    analyze.add_argument("--engine", choices=["auto", "llm", "rules"], default=None,
                         help="Analysis engine (default: TDIP_ENGINE or auto).")
    analyze.add_argument("--output-dir", default=None, help="Directory for the PDF and JSON outputs.")
    analyze.add_argument("--user", default=None, help="Identifier of the person running the analysis.")
    analyze.add_argument("--consent-share-with-ten-capital", action="store_true", dest="consent",
                         help="Explicitly consent to sending the structured record to TEN Capital.")
    analyze.add_argument("--no-email", action="store_true", dest="no_email",
                         help="Do not email the result to TEN Capital for this run.")
    analyze.add_argument("--quiet", action="store_true", help="Only print output file paths.")

    serve = sub.add_parser("serve", help="Run the web upload interface.")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    configure_logging("WARNING" if getattr(args, "quiet", False) else settings.log_level, settings.log_file)

    if args.command == "serve":
        import uvicorn

        uvicorn.run("app.web.server:app", host=args.host or settings.web_host, port=args.port or settings.web_port)
        return EXIT_OK

    from app.analysis.engine import AnalysisError
    from app.ingestion.errors import DeckIngestionError
    from app.integrations.ten_capital import resolve_consent
    from app.pipeline import run_diligence
    from app.reporting.pdf_generator import OnePageError
    from app.utils.validation import AnalysisValidationError

    try:
        run = run_diligence(args.deck, settings=settings, user=args.user, engine=args.engine,
                            output_dir=args.output_dir, consent=resolve_consent(args.consent is True),
                            send_email=not args.no_email)
    except DeckIngestionError as exc:
        print(f"Could not read deck: {exc}", file=sys.stderr)
        return EXIT_INGESTION
    except (AnalysisError, AnalysisValidationError) as exc:
        print(f"Analysis failed: {exc}", file=sys.stderr)
        return EXIT_ANALYSIS
    except OnePageError as exc:
        print(f"Report failed: {exc}", file=sys.stderr)
        return EXIT_REPORT

    analysis = run.analysis
    if not args.quiet:
        conclusion = analysis.investor_conclusion
        print()
        print(f"  {analysis.company.display_name()}")
        print(f"  Technical & IP score: {analysis.scores.overall_score} +/- {analysis.scores.uncertainty}"
              f"   Evidence confidence: {analysis.evidence_confidence}/100   Engine: {analysis.engine.name}")
        print(f"  Technical: {conclusion.technical_position.value}   IP: {conclusion.ip_position.value}"
              f"   Transaction readiness: {conclusion.transaction_readiness.value}")
        flags = analysis.red_flags
        print(f"  Red flags: {sum(f.severity.value == 'CRITICAL' for f in flags)} critical, "
              f"{sum(f.severity.value == 'HIGH' for f in flags)} high, {len(flags)} total")
        print(f"  TEN Capital record: consent {run.record.consent_status.value}; {run.submission.detail}")
        if run.email is not None:
            print(f"  Result email: {run.email.detail}")
        for warning in analysis.warnings + run.validation_warnings:
            print(f"  ! {warning}")
        print()
    print(f"PDF:    {run.pdf_path}")
    print(f"JSON:   {run.analysis_path}")
    print(f"Record: {run.record_path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
