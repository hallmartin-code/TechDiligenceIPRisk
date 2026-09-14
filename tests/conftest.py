from __future__ import annotations

from pathlib import Path

import pytest

from app.analysis.engine import analyze_deck
from app.config import Settings
from app.ingestion.extractor import load_deck
from samples.mock_decks import RISKY_COMPANY, STRONG_COMPANY, DeckSpec, SlideSpec, build_pdf, build_pptx

RULES = Settings(engine="rules", ten_capital_endpoint=None, ten_capital_api_key=None, default_user=None)


@pytest.fixture(scope="session")
def rules_settings() -> Settings:
    return RULES


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(engine="rules", output_dir=tmp_path / "out", ten_capital_endpoint=None,
                    ten_capital_api_key=None, default_user=None)


@pytest.fixture(scope="session")
def deck_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("decks")


@pytest.fixture(scope="session")
def strong_pptx(deck_dir: Path) -> Path:
    return build_pptx(STRONG_COMPANY, deck_dir / "helix.pptx")


@pytest.fixture(scope="session")
def strong_pdf(deck_dir: Path) -> Path:
    return build_pdf(STRONG_COMPANY, deck_dir / "helix.pdf")


@pytest.fixture(scope="session")
def risky_pptx(deck_dir: Path) -> Path:
    return build_pptx(RISKY_COMPANY, deck_dir / "quickmind.pptx")


@pytest.fixture(scope="session")
def risky_pdf(deck_dir: Path) -> Path:
    return build_pdf(RISKY_COMPANY, deck_dir / "quickmind.pdf")


def analyze_path(path: Path):
    deck = load_deck(path, RULES)
    return deck, analyze_deck(deck, RULES, engine="rules")


@pytest.fixture(scope="session")
def strong(strong_pptx):
    """(deck, analysis) for the technically strong company, from PPTX."""
    return analyze_path(strong_pptx)


@pytest.fixture(scope="session")
def risky(risky_pdf):
    """(deck, analysis) for the high-risk company, from PDF."""
    return analyze_path(risky_pdf)


@pytest.fixture
def make_deck(tmp_path: Path):
    counter = {"n": 0}

    def factory(*slides: tuple[str, list[str]], name: str = "Acme Robotics", fmt: str = "pptx") -> Path:
        counter["n"] += 1
        spec = DeckSpec(name, [SlideSpec(name, ["Company overview"])] + [SlideSpec(t, b) for t, b in slides])
        path = tmp_path / f"deck_{counter['n']}.{fmt}"
        return build_pptx(spec, path) if fmt == "pptx" else build_pdf(spec, path)

    return factory


@pytest.fixture
def analyze(make_deck):
    def run(*slides: tuple[str, list[str]], **kwargs):
        return analyze_path(make_deck(*slides, **kwargs))[1]

    return run
