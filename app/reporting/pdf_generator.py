"""One-page Technical & IP Diligence Review PDF (US Letter, portrait).

Fitting strategy: the story is built at successive density levels — each one
shortens lower-priority text, trims the risk list toward three items and
tightens type — and measured against the frame before rendering. The rendered
bytes are then checked with pypdf; a second page is never accepted. Diligence
questions and the investor conclusion are present at every level.
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from pypdf import PdfReader
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import BaseDocTemplate, Frame, KeepInFrame, PageTemplate, Paragraph, Spacer, Table, TableStyle

from app.config import LOGO_PATH, Settings, get_settings
from app.models.schemas import (
    CATEGORY_SHORT_LABELS,
    NOT_PROVIDED,
    Classification,
    DiligenceAnalysis,
    EvidenceStatus,
    Severity,
    SnapshotField,
    TransactionExposure,
)
from app.reporting import templates as t
from app.utils.logging import get_logger
from app.utils.text import shorten

log = get_logger("reporting.pdf")

PAGE_WIDTH, PAGE_HEIGHT = letter
MARGIN_X = 0.42 * inch
MARGIN_TOP = 0.34 * inch
MARGIN_BOTTOM = 0.52 * inch
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN_X
FRAME_HEIGHT = PAGE_HEIGHT - MARGIN_TOP - MARGIN_BOTTOM
FIT_TOLERANCE = 4.0


class OnePageError(RuntimeError):
    """The report could not be rendered on a single page."""


def generate_pdf(analysis: DiligenceAnalysis, output_path: str | Path, settings: Settings | None = None,
                 compiled_on: date | None = None) -> Path:
    settings = settings or get_settings()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = render_pdf_bytes(analysis, settings, compiled_on)
    output_path.write_bytes(data)
    log.info("One-page report written to %s", output_path)
    return output_path


def render_pdf_bytes(analysis: DiligenceAnalysis, settings: Settings | None = None,
                     compiled_on: date | None = None) -> bytes:
    settings = settings or get_settings()
    fonts = t.register_fonts(settings.font_dir)
    compiled_on = compiled_on or date.today()

    for density in t.DENSITIES:
        height = _measure(_story(analysis, density, fonts))
        if height > FRAME_HEIGHT - FIT_TOLERANCE:
            log.debug("Density %d needs %.0fpt of %.0fpt; compressing", density.level, height, FRAME_HEIGHT)
            continue
        data = _render(_story(analysis, density, fonts), analysis, fonts, compiled_on)
        if page_count(data) == 1:
            log.info("Report fitted at density level %d", density.level)
            return data

    log.warning("Content exceeded all density levels; shrinking to fit one page")
    story = _story(analysis, t.DENSITIES[-1], fonts)
    data = _render([KeepInFrame(CONTENT_WIDTH, FRAME_HEIGHT - FIT_TOLERANCE, story, mode="shrink")],
                   analysis, fonts, compiled_on)
    if page_count(data) != 1:
        raise OnePageError("The report could not be fitted to a single page.")
    return data


def page_count(data: bytes) -> int:
    return len(PdfReader(io.BytesIO(data)).pages)


def _measure(story: list) -> float:
    total = 0.0
    for flowable in story:
        _, height = flowable.wrap(CONTENT_WIDTH, FRAME_HEIGHT * 20)
        total += height + flowable.getSpaceBefore() + flowable.getSpaceAfter()
    return total


def _render(story: list, analysis: DiligenceAnalysis, fonts: t.FontSet, compiled_on: date) -> bytes:
    buffer = io.BytesIO()
    doc = BaseDocTemplate(
        buffer, pagesize=letter, leftMargin=MARGIN_X, rightMargin=MARGIN_X, topMargin=MARGIN_TOP,
        bottomMargin=MARGIN_BOTTOM, title=f"{t.DOC_TITLE} - {analysis.company.display_name()}",
        author=t.ORG_NAME, subject="Technical and IP diligence review", creator=t.ORG_NAME)
    frame = Frame(MARGIN_X, MARGIN_BOTTOM, CONTENT_WIDTH, FRAME_HEIGHT, leftPadding=0, rightPadding=0,
                  topPadding=0, bottomPadding=0, id="body")

    def footer(canvas, _doc) -> None:
        _draw_footer(canvas, fonts, compiled_on)

    doc.addPageTemplates([PageTemplate(id="one", frames=[frame], onPage=footer)])
    doc.build(list(story))
    return buffer.getvalue()


def _draw_footer(canvas, fonts: t.FontSet, compiled_on: date) -> None:
    """House footer: [Title]   [PAGE#]   Compiled on [DATE] by TEN Capital Network   [logo]."""
    text = (f"{t.DOC_TITLE}          {canvas.getPageNumber()}          Compiled on "
            f"{compiled_on.strftime('%B')} {compiled_on.day}, {compiled_on.year} by {t.ORG_NAME}    ")
    logo_w, logo_h = 48.0, 18.0
    text_w = stringWidth(text, fonts.regular, 7)
    x = (PAGE_WIDTH - text_w - (logo_w if LOGO_PATH.exists() else 0)) / 2
    y = 0.24 * inch
    canvas.saveState()
    canvas.setFont(fonts.regular, 7)
    canvas.setFillColor(t.MUTED)
    canvas.drawString(x, y + 5, text)
    if LOGO_PATH.exists():
        canvas.drawImage(str(LOGO_PATH), x + text_w, y, width=logo_w, height=logo_h, mask="auto",
                         preserveAspectRatio=True)
    canvas.restoreState()


# --- Story ---------------------------------------------------------------------


class _Styles:
    def __init__(self, density: t.Density, fonts: t.FontSet) -> None:
        body, small = density.body, density.small
        self.fonts = fonts
        self.body = ParagraphStyle("body", fontName=fonts.regular, fontSize=body, leading=body * 1.22,
                                   textColor=t.INK, spaceAfter=body * 0.28)
        self.small = ParagraphStyle("small", parent=self.body, fontSize=small, leading=small * 1.22,
                                    textColor=t.MUTED, spaceAfter=0)
        self.cell = ParagraphStyle("cell", parent=self.body, spaceAfter=0)
        self.section = ParagraphStyle("section", fontName=fonts.bold, fontSize=7.6, leading=9.2, textColor=t.NAVY)
        self.eyebrow = ParagraphStyle("eyebrow", fontName=fonts.bold, fontSize=7.2, leading=9, textColor=t.ORANGE)
        self.meta = ParagraphStyle("meta", fontName=fonts.regular, fontSize=6.8, leading=8.4, textColor=t.LIGHT_BLUE)
        self.tile = ParagraphStyle("tile", fontName=fonts.bold, fontSize=19, leading=21, textColor=t.WHITE,
                                   alignment=TA_CENTER)
        self.tile_label = ParagraphStyle("tile_label", fontName=fonts.regular, fontSize=5.9, leading=7.2,
                                         textColor=t.LIGHT_BLUE, alignment=TA_CENTER)
        self.center = ParagraphStyle("center", parent=self.cell, alignment=TA_CENTER, fontSize=small,
                                     leading=small * 1.18)
        self.chip = ParagraphStyle("chip", fontName=fonts.bold, fontSize=small, leading=small * 1.2,
                                   textColor=t.WHITE, alignment=TA_CENTER)


def _safe(text: str, fonts: t.FontSet) -> str:
    text = text or ""
    if not fonts.unicode:  # the core fonts only cover cp1252
        text = text.encode("cp1252", "replace").decode("cp1252")
    return escape(text)


def _rich(text: str, fonts: t.FontSet, limit: int) -> str:
    """Escape, shorten, and highlight NOT PROVIDED markers."""
    value = _safe(shorten(text or NOT_PROVIDED, limit), fonts)
    return value.replace(NOT_PROVIDED, f'<font color="#E85D26"><b>{NOT_PROVIDED}</b></font>')


def _section(title: str, styles: _Styles) -> Table:
    table = Table([[Paragraph(escape(title.upper()), styles.section)]], colWidths=[CONTENT_WIDTH])
    table.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), 0.9, t.ORANGE),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.6),
    ]))
    return table


def _column_header(title: str, styles: _Styles, width: float) -> Table:
    table = Table([[Paragraph(escape(title.upper()), styles.section)]], colWidths=[width])
    table.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), 0.9, t.ORANGE),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.6),
    ]))
    return table


def _story(analysis: DiligenceAnalysis, density: t.Density, fonts: t.FontSet) -> list:
    styles = _Styles(density, fonts)
    gap = density.gap
    return [
        _header(analysis, styles),
        Spacer(1, gap),
        _section("Company Snapshot", styles),
        Spacer(1, 2.5),
        _snapshot(analysis, styles),
        Spacer(1, 3),
        _score_strip(analysis, styles),
        Spacer(1, gap),
        _two_columns(analysis, density, styles),
        Spacer(1, gap),
        _section("Top Risks", styles),
        Spacer(1, 2.5),
        _risks(analysis, density, styles),
        Spacer(1, gap),
        _exit_and_questions(analysis, density, styles),
        Spacer(1, gap),
        _conclusion(analysis, density, styles),
        Spacer(1, 3),
        _basis(analysis, styles),
    ]


def _header(analysis: DiligenceAnalysis, styles: _Styles) -> Table:
    fonts = styles.fonts
    name = analysis.company.display_name()
    size = 15 if len(name) <= 42 else 12.5 if len(name) <= 80 else 10.5
    name_style = ParagraphStyle("name", fontName=fonts.bold, fontSize=size, leading=size * 1.15, textColor=t.WHITE)
    snap = analysis.company
    meta_parts = [shorten(v.value, 40) for v in (snap.sector, snap.stage) if v.provided]
    meta_parts.append(f"Source: {analysis.source.filename} ({analysis.source.slide_count} slides)")
    meta_parts.append(analysis.generated_at.strftime("%b %d, %Y"))
    left = [
        Paragraph("TECHNICAL &amp; IP DILIGENCE REVIEW", styles.eyebrow),
        Paragraph(_safe(shorten(name, 140), fonts), name_style),
        Paragraph(_safe(shorten("  ·  ".join(meta_parts), 150), fonts), styles.meta),
    ]
    scores = analysis.scores
    score_tile = [Paragraph(f'{scores.overall_score}<font size="8.5"> ±{scores.uncertainty}</font>', styles.tile),
                  Paragraph("TECH &amp; IP SCORE", styles.tile_label)]
    confidence_tile = [Paragraph(f'{analysis.evidence_confidence}<font size="8.5">/100</font>', styles.tile),
                       Paragraph("EVIDENCE CONFIDENCE", styles.tile_label)]
    tiles = Table([[score_tile, confidence_tile]], colWidths=[88, 88])
    tiles.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), t.NAVY_LIGHT), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEAFTER", (0, 0), (0, 0), 0.6, t.NAVY), ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]))
    header = Table([[left, tiles]], colWidths=[CONTENT_WIDTH - 190, 190])
    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), t.NAVY), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, 0), 10), ("RIGHTPADDING", (0, 0), (0, 0), 6),
        ("LEFTPADDING", (1, 0), (1, 0), 0), ("RIGHTPADDING", (1, 0), (1, 0), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 0), (-1, -1), 2, t.ORANGE),
    ]))
    return header


def _field(label: str, field: SnapshotField | str, styles: _Styles, limit: int = 48) -> Paragraph:
    fonts = styles.fonts
    if isinstance(field, str):
        value, provided, inferred = field, field != NOT_PROVIDED, False
    else:
        value, provided = field.value, field.provided
        inferred = field.classification in (Classification.INFERENCE, Classification.ASSUMPTION)
    if provided:
        text = f"<b>{_safe(shorten(value, limit), fonts)}</b>"
        if inferred:
            text += f' <font size="{styles.small.fontSize - 0.4}" color="#5B6573">(inferred)</font>'
    else:
        text = f'<font color="#5B6573"><i>Not provided</i></font>'
    return Paragraph(f'<font size="{styles.small.fontSize - 0.2}" color="#5B6573">{escape(label.upper())}</font><br/>'
                     f"{text}", styles.cell)


def _snapshot(analysis: DiligenceAnalysis, styles: _Styles) -> Table:
    snap = analysis.company
    funding = snap.raise_amount.value if snap.raise_amount.provided else ""
    if snap.funding_to_date.provided:
        funding = f"{funding} · {snap.funding_to_date.value}" if funding else snap.funding_to_date.value
    funding_field = SnapshotField(value=funding, classification=Classification.FACT) if funding else SnapshotField()
    customers = snap.customers.value if snap.customers.provided else ""
    if snap.customer_concentration.provided:
        customers = f"{customers} · {snap.customer_concentration.value}" if customers else snap.customer_concentration.value
    customers_field = (SnapshotField(value=customers, classification=snap.customers.classification
                                     if snap.customers.provided else Classification.FACT)
                       if customers else SnapshotField())
    maturity = analysis.development_maturity.level.value
    rows = [
        [_field("Sector", snap.sector, styles), _field("Stage", snap.stage, styles),
         _field("Revenue", snap.revenue, styles), _field("Growth", snap.growth, styles)],
        [_field("Gross Margin", snap.gross_margin, styles), _field("Customers", customers_field, styles, 60),
         _field("Funding / Raise", funding_field, styles, 60),
         _field("Development Maturity", maturity if maturity != "Not determinable" else NOT_PROVIDED, styles)],
    ]
    width = CONTENT_WIDTH / 4
    table = Table(rows, colWidths=[width] * 4)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), t.BACKGROUND), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.4, t.RULE), ("LINEAFTER", (0, 0), (-2, -1), 0.4, t.RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.4),
    ]))
    return table


def _score_strip(analysis: DiligenceAnalysis, styles: _Styles) -> Table:
    cells = []
    for item in analysis.scores.categories:
        color = "#8A94A3" if item.evidence_status == EvidenceStatus.NOT_PROVIDED else t.SCORE_COLORS[item.score]
        note = ('<br/><font size="4.8" color="#8A94A3">unverified</font>'
                if item.evidence_status == EvidenceStatus.NOT_PROVIDED else "")
        cells.append(Paragraph(
            f'<font size="{styles.small.fontSize - 0.3}" color="#5B6573">{escape(CATEGORY_SHORT_LABELS[item.category])}'
            f'</font><br/><font size="9.5" color="{color}"><b>{item.score}</b></font>'
            f'<font size="5.6" color="#8A94A3">/5 · {int(item.weight * 100)}%</font>{note}', styles.center))
    table = Table([cells], colWidths=[CONTENT_WIDTH / len(cells)] * len(cells))
    table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.4, t.RULE), ("LINEAFTER", (0, 0), (-2, -1), 0.4, t.RULE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 1.6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8), ("LEFTPADDING", (0, 0), (-1, -1), 1),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1),
    ]))
    return table


def _lines(rows: list[tuple[str, str]], limit: int, styles: _Styles) -> list[Paragraph]:
    return [Paragraph(f'<font color="#1B2A4A"><b>{escape(label)}:</b></font> {_rich(text, styles.fonts, limit)}',
                      styles.body) for label, text in rows]


def _two_columns(analysis: DiligenceAnalysis, density: t.Density, styles: _Styles) -> Table:
    gutter = 12
    col = (CONTENT_WIDTH - gutter) / 2
    maturity = analysis.development_maturity
    rationale = maturity.rationale.removeprefix(f"{maturity.level.value}: ")
    tech_rows = [
        ("Architecture", analysis.technical_architecture.summary),
        ("Development maturity", f"{maturity.level.value} — {rationale}"),
        ("Scalability", analysis.scalability.summary),
        ("Engineering org", analysis.engineering_organization.summary),
    ]
    if density.security_line:
        tech_rows.append(("Security / compliance", analysis.cybersecurity.summary))
    if density.ai_line and analysis.ai_data.applicable:
        tech_rows.append(("AI / data", analysis.ai_data.proprietary_assessment))
    ip = analysis.intellectual_property
    ip_rows = [
        ("Core IP", ip.core_ip), ("Ownership", ip.ownership), ("Defensibility", ip.defensibility),
        ("Patents", ip.patents), ("Trade secrets", ip.trade_secrets), ("Software / data", ip.software_data),
        ("Third-party", ip.third_party),
    ]
    left = [_column_header("Technical Assessment", styles, col), Spacer(1, 2.5)] + \
        _lines(tech_rows, density.summary_chars, styles)
    right = [_column_header("Intellectual Property", styles, col), Spacer(1, 2.5)] + \
        _lines(ip_rows, density.ip_chars, styles)
    table = Table([[left, "", right]], colWidths=[col, gutter, col])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def _risks(analysis: DiligenceAnalysis, density: t.Density, styles: _Styles) -> Table:
    fonts = styles.fonts
    widths = [52, 262, CONTENT_WIDTH - 314]
    header = [Paragraph("<b>SEVERITY</b>", styles.small), Paragraph("<b>ISSUE &amp; EVIDENCE</b>", styles.small),
              Paragraph("<b>BUSINESS CONSEQUENCE &amp; MITIGATION</b>", styles.small)]
    rows = [header]
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, 0), 0.5, t.NAVY),
        ("LEFTPADDING", (0, 0), (-1, -1), 3.5), ("RIGHTPADDING", (0, 0), (-1, -1), 3.5),
        ("TOPPADDING", (0, 0), (-1, -1), 1.8), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
    ]
    flags = analysis.red_flags[: density.risks]
    if not flags:
        rows.append([Paragraph("—", styles.center),
                     Paragraph("No material red flags identified from deck evidence.", styles.cell),
                     Paragraph("Verify deck claims against the data room.", styles.cell)])
    for index, flag in enumerate(flags, start=1):
        evidence = ""
        if density.risk_evidence_chars:
            evidence = (f'<br/><font size="{styles.small.fontSize}" color="#5B6573">{flag.classification.value} · '
                        f'{_safe(shorten(flag.evidence, density.risk_evidence_chars), fonts)}</font>')
        consequence = _safe(shorten(flag.business_consequence, density.risk_consequence_chars), fonts)
        if density.risk_mitigation_chars:
            consequence += (f' <font color="#1B2A4A"><b>Mitigate:</b></font> '
                            f'{_safe(shorten(flag.mitigation, density.risk_mitigation_chars), fonts)}')
        rows.append([
            Paragraph(flag.severity.value, styles.chip),
            Paragraph(f"<b>{_safe(shorten(flag.issue, density.risk_issue_chars), fonts)}</b>{evidence}", styles.cell),
            Paragraph(consequence, styles.cell),
        ])
        commands.append(("BACKGROUND", (0, index), (0, index), t.SEVERITY_COLORS[flag.severity]))
        if index % 2 == 0:
            commands.append(("BACKGROUND", (1, index), (-1, index), t.BACKGROUND))
        commands.append(("LINEBELOW", (0, index), (-1, index), 0.3, t.RULE))
    table = Table(rows, colWidths=widths)
    table.setStyle(TableStyle(commands))
    return table


def _exit_and_questions(analysis: DiligenceAnalysis, density: t.Density, styles: _Styles) -> Table:
    fonts = styles.fonts
    gutter = 12
    col = (CONTENT_WIDTH - gutter) / 2
    exit_section = analysis.exit_readiness
    left = [_column_header("Exit / Deal-Closing Exposure", styles, col), Spacer(1, 2.5),
            Paragraph(_rich(exit_section.summary, fonts, density.exit_summary_chars), styles.body)]
    for item in exit_section.exposures[: density.exposures]:
        types = [x.value for x in item.exposure_types if x != TransactionExposure.DILIGENCE_QUESTION] or \
            [TransactionExposure.DILIGENCE_QUESTION.value]
        color = "#" + t.SEVERITY_COLORS[item.severity].hexval()[2:]
        left.append(Paragraph(
            f'<font color="{color}"><b>•</b></font> '
            f"<b>{_safe(shorten(item.issue, density.exposure_chars), fonts)}</b>"
            f' <font color="#5B6573">› {escape(", ".join(types[:3]))}</font>', styles.body))

    right = [_column_header("Priority Diligence Questions", styles, col), Spacer(1, 2.5)]
    for question in analysis.diligence_questions[:5]:
        right.append(Paragraph(
            f'<font color="#E85D26"><b>{question.priority}.</b></font> '
            f"{_safe(shorten(question.question, density.question_chars), fonts)} "
            f'<font size="{styles.small.fontSize}" color="#5B6573">[{_safe(question.audience, fonts)}]</font>',
            styles.body))
    table = Table([[left, "", right]], colWidths=[col, gutter, col])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def _conclusion(analysis: DiligenceAnalysis, density: t.Density, styles: _Styles) -> Table:
    fonts = styles.fonts
    conclusion = analysis.investor_conclusion

    def chip(label: str, value: str, color: str) -> Paragraph:
        return Paragraph(f'<font size="{styles.small.fontSize - 0.2}" color="#5B6573">{escape(label)}</font><br/>'
                         f'<font size="{styles.body.fontSize + 1.6}" color="{color}"><b>{escape(value)}</b></font>',
                         styles.cell)

    chips = Table([[
        chip("TECHNICAL POSITION", conclusion.technical_position.value, t.RATING_COLORS[conclusion.technical_position]),
        chip("IP POSITION", conclusion.ip_position.value, t.RATING_COLORS[conclusion.ip_position]),
        chip("TRANSACTION READINESS", conclusion.transaction_readiness.value,
             t.READINESS_COLORS[conclusion.transaction_readiness]),
    ]], colWidths=[118, 118, CONTENT_WIDTH - 8 - 236 - 18])
    chips.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    body = [Paragraph("INVESTOR CONCLUSION", styles.section), Spacer(1, 2), chips,
            Paragraph(_safe(shorten(conclusion.summary, density.conclusion_chars), fonts), styles.cell)]
    table = Table([["", body]], colWidths=[4, CONTENT_WIDTH - 4])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), t.ORANGE), ("BACKGROUND", (1, 0), (1, 0), t.TINT),
        ("LEFTPADDING", (1, 0), (1, 0), 9), ("RIGHTPADDING", (1, 0), (1, 0), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5.5),
        ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (0, 0), (0, 0), 0),
    ]))
    return table


def _basis(analysis: DiligenceAnalysis, styles: _Styles) -> Paragraph:
    counts = analysis.classification_counts()
    engine = analysis.engine
    engine_label = f"Claude ({engine.model})" if engine.name == "llm" else "deterministic rules"
    critical = len(analysis.flags_at(Severity.CRITICAL))
    high = len(analysis.flags_at(Severity.HIGH))
    text = (f"Evidence basis: {counts['FACT']} facts · {counts['INFERENCE']} inferences · "
            f"{counts['ASSUMPTION']} assumptions · {counts['NOT PROVIDED']} items not provided · "
            f"{critical} critical / {high} high flags ({len(analysis.red_flags)} total in JSON) · slide refs S# "
            f"· engine: {engine_label}. {t.DISCLAIMER}")
    style = ParagraphStyle("basis", parent=styles.small, fontSize=5.6, leading=6.9)
    return Paragraph(_safe(text, styles.fonts), style)
