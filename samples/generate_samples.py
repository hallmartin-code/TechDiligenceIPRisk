"""Regenerate the sample decks and sample outputs.

    python -m samples.generate_samples

Uses the deterministic rules engine, so no API key is needed and the output is
reproducible (apart from timestamps and record ids).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from app.config import Settings
from app.pipeline import run_diligence
from samples.mock_decks import RISKY_COMPANY, STRONG_COMPANY, build_pdf, build_pptx

ROOT = Path(__file__).resolve().parent


def main() -> None:
    decks, output = ROOT / "decks", ROOT / "output"
    decks.mkdir(exist_ok=True)
    output.mkdir(exist_ok=True)

    strong_pptx = build_pptx(STRONG_COMPANY, decks / "helix_grid_systems_series_b.pptx")
    build_pdf(STRONG_COMPANY, decks / "helix_grid_systems_series_b.pdf")
    build_pptx(RISKY_COMPANY, decks / "quickmind_ai_seed_extension.pptx")
    risky_pdf = build_pdf(RISKY_COMPANY, decks / "quickmind_ai_seed_extension.pdf")

    settings = Settings(engine="rules", ten_capital_endpoint=None, ten_capital_api_key=None)
    for deck, stem in ((strong_pptx, "helix_grid_systems"), (risky_pdf, "quickmind_ai")):
        with tempfile.TemporaryDirectory() as tmp:
            run = run_diligence(deck, settings=settings, output_dir=tmp, user="analyst@example.com")
            shutil.copyfile(run.pdf_path, output / f"{stem}_tech_ip_review.pdf")
            shutil.copyfile(run.analysis_path, output / f"{stem}_analysis.json")
            shutil.copyfile(run.record_path, output / f"{stem}_ten_capital_record.json")
        print(f"{stem}: score {run.analysis.scores.overall_score} ± {run.analysis.scores.uncertainty}, "
              f"{run.analysis.investor_conclusion.transaction_readiness.value}")


if __name__ == "__main__":
    main()
