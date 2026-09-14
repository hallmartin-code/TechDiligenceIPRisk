"""Visual system for the one-page report: palette, fonts, density levels, labels."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from app.config import ASSETS_DIR
from app.models.schemas import PositionRating, Severity, TransactionReadiness
from app.utils.logging import get_logger

log = get_logger("reporting")

DOC_TITLE = "Technical & IP Diligence Review"
ORG_NAME = "TEN Capital Network"
DISCLAIMER = ("Deck-based screening only; not legal advice, a patent-validity or freedom-to-operate opinion, "
              "or a security audit. Verify all findings against the data room.")

# TEN Capital house palette, plus severity and rating accents.
NAVY = colors.HexColor("#1B2A4A")
NAVY_LIGHT = colors.HexColor("#26395F")
ORANGE = colors.HexColor("#E85D26")
LIGHT_BLUE = colors.HexColor("#C8D6E8")
BACKGROUND = colors.HexColor("#F7F9FC")
TINT = colors.HexColor("#EEF3F9")
INK = colors.HexColor("#1F2733")
MUTED = colors.HexColor("#5B6573")
RULE = colors.HexColor("#D5DDE8")
WHITE = colors.white

SEVERITY_COLORS = {
    Severity.CRITICAL: colors.HexColor("#B42318"),
    Severity.HIGH: colors.HexColor("#E85D26"),
    Severity.MEDIUM: colors.HexColor("#A86B12"),
    Severity.LOW: colors.HexColor("#5B7083"),
}
SCORE_COLORS = {5: "#1F7A4D", 4: "#3F8A3A", 3: "#A86B12", 2: "#D9531E", 1: "#B42318"}
RATING_COLORS = {
    PositionRating.STRONG: "#1F7A4D", PositionRating.ADEQUATE: "#3F8A3A", PositionRating.MIXED: "#A86B12",
    PositionRating.WEAK: "#D9531E", PositionRating.HIGH_RISK: "#B42318",
}
READINESS_COLORS = {
    TransactionReadiness.READY: "#1F7A4D", TransactionReadiness.MOSTLY_READY: "#3F8A3A",
    TransactionReadiness.MATERIAL_DILIGENCE: "#A86B12", TransactionReadiness.SIGNIFICANT_REMEDIATION: "#B42318",
}


@dataclass(frozen=True)
class Density:
    """One compression level. The generator tries levels in order until the page fits."""
    level: int
    body: float
    small: float
    summary_chars: int
    ip_chars: int
    risks: int
    risk_issue_chars: int
    risk_evidence_chars: int
    risk_consequence_chars: int
    risk_mitigation_chars: int
    question_chars: int
    exposures: int
    exposure_chars: int
    exit_summary_chars: int
    conclusion_chars: int
    security_line: bool
    ai_line: bool
    gap: float


DENSITIES: tuple[Density, ...] = (
    Density(0, 7.3, 6.1, 240, 190, 5, 110, 130, 150, 110, 180, 4, 80, 220, 520, True, True, 6.0),
    Density(1, 7.1, 6.0, 200, 160, 5, 100, 100, 130, 90, 165, 4, 70, 180, 480, True, True, 5.5),
    Density(2, 6.9, 5.9, 170, 135, 4, 95, 80, 115, 70, 150, 3, 65, 160, 440, True, False, 5.0),
    Density(3, 6.7, 5.8, 140, 115, 4, 90, 60, 100, 0, 135, 3, 60, 130, 400, False, False, 4.5),
    Density(4, 6.5, 5.6, 115, 95, 3, 85, 45, 90, 0, 120, 3, 55, 110, 360, False, False, 4.0),
    Density(5, 6.3, 5.5, 95, 80, 3, 80, 0, 80, 0, 110, 2, 50, 90, 320, False, False, 3.5),
)


@dataclass(frozen=True)
class FontSet:
    regular: str
    bold: str
    italic: str
    unicode: bool


_REGISTERED: FontSet | None = None


def register_fonts(font_dir: Path | None = None) -> FontSet:
    """Use Open Sans when the TTFs are available; otherwise the built-in Helvetica family."""
    global _REGISTERED
    if _REGISTERED is not None:
        return _REGISTERED
    for directory in [d for d in (font_dir, ASSETS_DIR / "fonts") if d]:
        files = {name: Path(directory) / f"OpenSans-{name}.ttf" for name in ("Regular", "Bold", "Italic")}
        if all(path.exists() for path in files.values()):
            try:
                for name, path in files.items():
                    pdfmetrics.registerFont(TTFont(f"OpenSans-{name}", str(path)))
                pdfmetrics.registerFontFamily("OpenSans", normal="OpenSans-Regular", bold="OpenSans-Bold",
                                              italic="OpenSans-Italic", boldItalic="OpenSans-Bold")
                _REGISTERED = FontSet("OpenSans-Regular", "OpenSans-Bold", "OpenSans-Italic", True)
                return _REGISTERED
            except Exception as exc:  # corrupt font file: fall back rather than fail the report
                log.warning("Open Sans could not be registered from %s: %s", directory, exc)
    _REGISTERED = FontSet("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", False)
    return _REGISTERED
