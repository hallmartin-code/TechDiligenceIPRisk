"""Deck loading (validation + format dispatch) and company-snapshot extraction.

`load_deck` is the single entry point for every upload: it checks existence,
size, extension and file signature before any parser runs, and converts every
parser failure into a `DeckIngestionError` with a user-readable message.

`extract_company_snapshot` pulls the headline business facts (stage, revenue,
growth, margin, customers, raise) with their slide numbers. Projections are
excluded: a forecast is never reported as revenue.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, get_settings
from app.ingestion.document import DeckDocument
from app.ingestion.errors import (
    DeckIngestionError,
    DeckTooLargeError,
    MalformedDeckError,
    UnsupportedDeckError,
)
from app.ingestion.pdf_parser import parse_pdf
from app.ingestion.ppt_parser import looks_like_pptx, parse_ppt, parse_pptx
from app.models.schemas import Classification, CompanySnapshot, SnapshotField
from app.utils.context import is_projection
from app.utils.logging import get_logger
from app.utils.text import clean, shorten, split_units

log = get_logger("ingestion")

SUPPORTED_SUFFIXES = {".pdf": "pdf", ".pptx": "pptx", ".ppt": "ppt"}
_OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")


def load_deck(path: str | Path, settings: Settings | None = None) -> DeckDocument:
    settings = settings or get_settings()
    path = Path(path)
    if not path.is_file():
        raise DeckIngestionError(f"File not found: {path.name}")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise UnsupportedDeckError(
            f"Unsupported file type '{suffix or 'none'}'. Upload a PDF, PPT or PPTX pitch deck."
        )

    size = path.stat().st_size
    if size == 0:
        raise MalformedDeckError(f"{path.name} is empty.")
    if size > settings.max_upload_mb * 1024 * 1024:
        raise DeckTooLargeError(
            f"{path.name} is {size / 1_048_576:.1f} MB; the limit is {settings.max_upload_mb:g} MB."
        )

    data = path.read_bytes()
    kind = _detect_kind(path, suffix, data)
    log.info("Parsing %s as %s (%d bytes)", path.name, kind, size)

    try:
        if kind == "pdf":
            slides, meta_title, warnings = parse_pdf(path, settings.max_slides)
        elif kind == "pptx":
            slides, meta_title, warnings = parse_pptx(path, settings.max_slides)
        else:
            slides, meta_title, warnings = parse_ppt(path, settings.max_slides, settings.soffice_path)
    except DeckIngestionError as exc:
        log.error("Ingestion failed for %s: %s", path.name, exc)
        raise
    except Exception as exc:  # never let a parser bug escape as a raw traceback
        log.exception("Unexpected parser failure for %s", path.name)
        raise MalformedDeckError(f"{path.name} could not be parsed ({type(exc).__name__}).") from exc

    if not slides:
        raise MalformedDeckError(f"{path.name} contains no pages or slides.")

    deck = DeckDocument(
        filename=path.name,
        file_type=kind,
        sha256=hashlib.sha256(data).hexdigest(),
        slides=slides,
        metadata_title=meta_title,
        warnings=list(warnings),
    )
    for slide in slides:
        deck.warnings.extend(slide.warnings)
    image_only = deck.image_only_slides()
    if image_only:
        deck.warnings.append(
            "No extractable text on slide(s) " + ", ".join(map(str, image_only))
            + " (image-only; OCR not performed). Content on those slides was not assessed."
        )
    if deck.text_characters == 0:
        deck.warnings.append("The deck contains no machine-readable text; findings will be NOT PROVIDED.")
    for warning in deck.warnings:
        log.warning("%s: %s", path.name, warning)
    return deck


def _detect_kind(path: Path, suffix: str, data: bytes) -> str:
    head = data[:1024]
    if suffix == ".pdf":
        if b"%PDF" not in head:
            raise MalformedDeckError(f"{path.name} does not look like a PDF (missing PDF header).")
        return "pdf"
    if head.startswith(b"PK"):
        if looks_like_pptx(path):
            return "pptx"
        raise MalformedDeckError(f"{path.name} is a ZIP archive but not a PowerPoint presentation.")
    if head.startswith(_OLE_SIGNATURE):
        return "ppt"
    raise MalformedDeckError(f"{path.name} does not look like a PowerPoint file (unrecognised signature).")


# --- Snapshot extraction -------------------------------------------------------

_MONEY = r"\$\s?\d[\d,]*(?:\.\d+)?\s*(?:k|mm|m|bn|b|million|billion|thousand)?\b"
_GENERIC_TITLES = re.compile(
    r"^(?:pitch ?deck|investor (?:presentation|deck|update)|confidential|presentation|company "
    r"overview|agenda|disclaimer|forward[- ]looking statements?|powerpoint presentation|"
    r"untitled|slide \d+|welcome|introduction)$",
    re.IGNORECASE,
)

SECTOR_KEYWORDS: dict[str, list[str]] = {
    "AI / Machine Learning": [r"\bai\b", r"artificial intelligence", r"machine learning", r"\bllms?\b",
                              r"generative", r"copilots?", r"agentic", r"neural", r"computer vision", r"\bnlp\b"],
    "Enterprise SaaS": [r"\bsaas\b", r"enterprise software", r"\bb2b\b", r"subscription", r"\barr\b"],
    "Healthcare / Digital Health": [r"patients?", r"clinical", r"hospitals?", r"health systems?", r"\behr\b",
                                    r"healthcare"],
    "Medical Devices": [r"\bfda\b", r"510\(k\)", r"medical devices?", r"de novo", r"implant"],
    "Biotech / Life Sciences": [r"therapeutics?", r"\bdrugs?\b", r"preclinical", r"phase (?:1|2|3|i|ii|iii)\b",
                                r"biologics?", r"molecules?"],
    "Fintech": [r"payments?", r"banking", r"lending", r"fintech", r"financial institutions?"],
    "Insurtech": [r"insurance", r"insurers?", r"\bclaims\b", r"underwriting", r"carriers?"],
    "Cybersecurity": [r"cybersecurity", r"threat detection", r"endpoint security", r"\bsiem\b", r"\bsoc\b analysts?"],
    "Energy / Climate": [r"\bgrid\b", r"utilit(?:y|ies)", r"\benergy\b", r"solar", r"batter(?:y|ies)", r"carbon",
                         r"decarboni"],
    "Industrial IoT / Hardware": [r"\biot\b", r"sensors?", r"hardware", r"edge devices?", r"firmware", r"embedded"],
    "Logistics / Supply Chain": [r"logistics", r"supply chain", r"freight", r"warehous", r"fleet"],
    "Marketplace / Consumer": [r"marketplace", r"consumers?", r"e-?commerce", r"\bd2c\b", r"retail"],
    "Defense / Government": [r"defen[cs]e", r"government", r"federal", r"\bdod\b", r"public sector"],
    "Education": [r"education", r"students?", r"edtech"],
    "Real Estate / Proptech": [r"real estate", r"proptech", r"construction"],
}


@dataclass
class _Unit:
    slide: int
    title: str
    text: str


def _units(deck: DeckDocument) -> list[_Unit]:
    units = []
    for slide in deck.slides:
        for unit in split_units(slide.full_text):
            units.append(_Unit(slide.number, slide.title, unit))
    return units


def _fact(value: str, unit: _Unit, classification: Classification = Classification.FACT) -> SnapshotField:
    return SnapshotField(value=shorten(clean(value), 90), classification=classification,
                         slides=[unit.slide], excerpt=shorten(unit.text, 240))


def _first(units: list[_Unit], patterns: list[str], skip_projection: bool = True,
           prefer: str | None = None) -> tuple[re.Match, _Unit] | None:
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    ordered = units
    if prefer:
        preferred = re.compile(prefer, re.IGNORECASE)
        ordered = sorted(units, key=lambda u: 0 if preferred.search(u.text) else 1)
    for unit in ordered:
        if skip_projection and is_projection(unit.text):
            continue
        for pattern in compiled:
            match = pattern.search(unit.text)
            if match:
                return match, unit
    return None


def _money(value: str) -> str:
    value = re.sub(r"\s+", "", value)
    return re.sub(r"(mm|m|bn|b|k)$", lambda m: m.group(1).upper(), value, flags=re.IGNORECASE)


def extract_company_snapshot(deck: DeckDocument) -> CompanySnapshot:
    units = _units(deck)
    snap = CompanySnapshot()
    snap.name = _company_name(deck)
    snap.sector = _sector(deck)

    found = _first(units, [r"\b(pre-?seed|seed(?: extension| round)?|series [a-f]\d?(?: extension)?|"
                           r"growth equity|bridge round|pre-series [a-f])\b"],
                   skip_projection=False, prefer=r"rais|round|seeking|\bask\b")
    if found:
        snap.stage = _fact(found[0].group(1).title().replace("Pre-Seed", "Pre-seed"), found[1])

    if re.search(r"\bpre-?revenue\b", deck.full_text, re.IGNORECASE):
        unit = next(u for u in units if re.search(r"\bpre-?revenue\b", u.text, re.IGNORECASE))
        snap.revenue = _fact("Pre-revenue", unit)
    else:
        kinds = r"(ARR|annual recurring revenue|annuali[sz]ed revenue|run[- ]rate|MRR|revenue|sales|bookings)"
        found = _first(units, [rf"(?P<m>{_MONEY})\s*(?:in\s+)?{kinds}",
                               rf"{kinds}(?:\s+[\w-]+){{0,3}}?\s*(?:of|:|=|at|reached|was|is|totaled|~)?\s*(?P<m>{_MONEY})"],
                       prefer=r"\barr\b|recurring")
        if found:
            match, unit = found
            kind = next(g for g in match.groups() if g and not g.startswith("$"))
            label = "ARR" if kind.upper() in {"ARR", "ANNUAL RECURRING REVENUE"} else kind.lower()
            snap.revenue = _fact(f"{_money(match.group('m'))} {label}", unit)

    found = _first(units, [
        r"(?P<g>\d[\d,.]*\s*(?:%|x))\s*(?:yoy|y/y|year[- ]over[- ]year|mom|m/m|month[- ]over[- ]month|qoq)"
        r"(?:\s+(?:revenue|arr))?(?:\s+growth)?",
        r"(?P<g>\d[\d,.]*\s*(?:%|x))\s+(?:annual |revenue |arr )?growth",
        r"(?:grew|growing|growth of|increased)\s+(?:[\w$]+\s+){0,3}?(?P<g>\d[\d,.]*\s*(?:%|x))",
    ])
    if found:
        snap.growth = _fact(found[0].group(0), found[1])

    found = _first(units, [r"(?P<v>\d{1,3}(?:\.\d+)?)\s*%\s*gross margins?",
                           r"gross margins?\s*(?:of|:|is|at|~|=|were|was)?\s*(?P<v>\d{1,3}(?:\.\d+)?)\s*%"])
    if found:
        snap.gross_margin = _fact(f"{found[0].group('v')}%", found[1])

    found = _first(units, [
        r"(?P<n>\d[\d,]*\+?)\s+(?:(?:paying|enterprise|active|b2b|commercial|pilot|live|signed|"
        r"production)\s+)*(?:customers|clients|logos|hospitals|health systems|utilities|brands|"
        r"accounts|carriers|banks|insurers|design partners)\b",
        r"customers include[s]?\s*:?\s*(?P<list>[^.\n]{3,80})",
    ], skip_projection=True)
    if found:
        snap.customers = _fact(found[0].group(0), found[1])

    found = _first(units, [
        r"(?:largest|top|single|biggest|one)\s+(?:\w+\s+){0,2}?(?:customer|client|pilot|account)"
        r"[^.\n]{0,40}?(?P<p>\d{1,3})\s*%",
        r"(?P<p>\d{1,3})\s*%\s*of (?:our |total )?revenue (?:comes? |is )?from (?:our |a |one |the )?"
        r"(?:largest|top|single|one|biggest)",
    ], skip_projection=False)
    if found:
        snap.customer_concentration = _fact(f"Largest customer {found[0].group('p')}% of revenue", found[1])

    found = _first(units, [
        rf"(?:raising|seeking|looking to raise|raise of|round of|the ask)[:\s]+(?:a |an |up to )?(?P<m>{_MONEY})",
        rf"(?P<m>{_MONEY})\s+(?:pre-?seed|seed|series [a-f]|round|raise|bridge)",
    ], skip_projection=False)
    if found:
        match, unit = found
        stage = snap.stage.value if snap.stage.provided else ""
        value = _money(match.group("m")) + (f" {stage}" if stage and stage.lower() not in match.group(0).lower() else "")
        if stage and stage.lower() in match.group(0).lower():
            value = f"{_money(match.group('m'))} {stage}"
        snap.raise_amount = _fact(value, unit)

    found = _first([u for u in units if not re.search(r"\braising\b", u.text, re.IGNORECASE)],
                   [rf"(?:raised|funding to date|total funding|previously raised)[^$\n.]{{0,30}}(?P<m>{_MONEY})"],
                   skip_projection=False)
    if found:
        snap.funding_to_date = _fact(f"{_money(found[0].group('m'))} raised to date", found[1])

    found = _first(units, [
        r"(?P<n>\d{1,4})\+?\s+(?:full-time\s+)?(?:in-house\s+)?(?:software\s+)?(?:engineers|developers)\b",
        r"engineering team of (?P<n>\d{1,4})",
        r"(?P<n>\d{1,4})-person (?:engineering|development|dev|tech) team",
        r"(?P<n>\d{1,4})\s+in-house (?:developers?|engineers?)",
    ], skip_projection=True)
    if found:
        count = int(found[0].group("n"))
        snap.engineering_team = _fact(f"{count} engineer{'s' if count != 1 else ''}", found[1])

    product_units = [u for u in units if re.search(r"product|solution|platform|what we do|overview|how it works",
                                                   u.title, re.IGNORECASE) and len(u.text) >= 25
                     and u.text != u.title]
    if product_units:
        snap.product = _fact(product_units[0].text, product_units[0])
    return snap


def _company_name(deck: DeckDocument) -> SnapshotField:
    first = deck.slides[0]
    candidates = [first.title] + first.text.splitlines()[:2]
    for candidate in candidates:
        candidate = re.split(r"\s+[|–—-]\s+|\s*:\s+", clean(candidate))[0].strip()
        if candidate and not _GENERIC_TITLES.match(candidate) and 2 <= len(candidate) <= 160:
            return SnapshotField(value=candidate, classification=Classification.FACT,
                                 slides=[1], excerpt=shorten(first.full_text, 200))
    meta = clean(deck.metadata_title)
    if meta and not _GENERIC_TITLES.match(meta) and not meta.lower().endswith((".pptx", ".pdf")):
        return SnapshotField(value=shorten(meta, 160), classification=Classification.INFERENCE,
                             excerpt="Document metadata title")
    stem = re.sub(r"[_-]+", " ", Path(deck.filename).stem).strip()
    return SnapshotField(value=shorten(stem.title(), 160) or "Unnamed company",
                         classification=Classification.ASSUMPTION, excerpt="Derived from file name")


def _sector(deck: DeckDocument) -> SnapshotField:
    scores: dict[str, tuple[int, list[int]]] = {}
    for sector, patterns in SECTOR_KEYWORDS.items():
        total, slides = 0, set()
        for slide in deck.slides:
            text = slide.full_text
            hits = sum(len(re.findall(p, text, re.IGNORECASE)) for p in patterns)
            if hits:
                total += hits
                slides.add(slide.number)
        if total >= 2:
            scores[sector] = (total, sorted(slides))
    if not scores:
        return SnapshotField()
    ranked = sorted(scores.items(), key=lambda item: -item[1][0])
    chosen = [ranked[0]]
    if len(ranked) > 1 and ranked[1][1][0] >= max(3, ranked[0][1][0] * 0.6):
        chosen.append(ranked[1])
    slides = sorted({s for _, (_, sl) in chosen for s in sl})[:6]
    return SnapshotField(value=" / ".join(name.split(" / ")[0] for name, _ in chosen),
                         classification=Classification.INFERENCE, slides=slides,
                         excerpt="Inferred from sector keywords in the deck")
