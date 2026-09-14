"""PDF deck parsing: per-page text, detected slide title, tables and image counts.

pdfplumber is the primary reader because it exposes font sizes (for titles)
and ruled tables. If it fails on a damaged file, pypdf is tried for plain text
so a partially broken deck still yields evidence, with the failure logged and
recorded as a parse warning.
"""

from __future__ import annotations

import statistics
from pathlib import Path

from app.ingestion.document import Slide
from app.ingestion.errors import DeckIngestionError, DeckTooLargeError, MalformedDeckError
from app.utils.logging import get_logger
from app.utils.text import clean

log = get_logger("ingestion.pdf")


def parse_pdf(path: Path, max_slides: int) -> tuple[list[Slide], str, list[str]]:
    """Return (slides, metadata title, document-level warnings)."""
    warnings: list[str] = []
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            page_count = len(pdf.pages)
            if page_count > max_slides:
                raise DeckTooLargeError(
                    f"{path.name} has {page_count} pages; the limit is {max_slides}."
                )
            metadata_title = str((pdf.metadata or {}).get("Title") or "")
            slides = [_parse_page(page, index) for index, page in enumerate(pdf.pages, 1)]
        return slides, clean(metadata_title), warnings
    except DeckIngestionError:
        raise
    except Exception as exc:  # pdfminer raises a wide range of types on bad input
        if "password" in str(exc).lower() or "encrypt" in type(exc).__name__.lower():
            raise MalformedDeckError(
                f"{path.name} is password-protected. Remove the password and upload again."
            ) from exc
        log.warning("pdfplumber could not read %s (%s: %s); trying pypdf",
                    path.name, type(exc).__name__, exc)
        warnings.append(f"Primary PDF parser failed ({type(exc).__name__}); used text-only fallback.")
        slides, title = _parse_with_pypdf(path, max_slides)
        return slides, title, warnings


def _parse_page(page, number: int) -> Slide:
    slide = Slide(number=number)
    try:
        text = page.extract_text() or ""
    except Exception as exc:
        log.warning("Text extraction failed on page %d: %s", number, exc)
        slide.warnings.append(f"S{number}: text extraction failed ({type(exc).__name__}).")
        text = ""

    try:
        words = page.extract_words(extra_attrs=["size"], use_text_flow=True)
        slide.title = _title_from_words(words, float(page.height))
    except Exception as exc:
        log.debug("Title detection failed on page %d: %s", number, exc)

    try:
        for table in page.extract_tables() or []:
            rows = [[clean(cell or "") for cell in row] for row in table if row]
            rows = [row for row in rows if any(row)]
            if rows:
                slide.tables.append(rows)
    except Exception as exc:
        log.warning("Table extraction failed on page %d: %s", number, exc)
        slide.warnings.append(f"S{number}: table extraction failed ({type(exc).__name__}).")

    try:
        slide.image_count = len(page.images)
    except Exception:  # pragma: no cover - defensive
        slide.image_count = 0

    text = clean(text)
    if slide.title and text.startswith(slide.title):
        text = text[len(slide.title):].lstrip()
    slide.text = text
    return slide


def _title_from_words(words: list[dict], page_height: float) -> str:
    """Pick the largest-font line in the top portion of the page as the slide title."""
    if not words:
        return ""
    sizes = [float(w.get("size", 0) or 0) for w in words]
    median = statistics.median(sizes) if sizes else 0
    lines: dict[tuple[int, float], list[dict]] = {}
    for word in words:
        key = (round(float(word["top"]) / 4), round(float(word.get("size", 0) or 0), 1))
        lines.setdefault(key, []).append(word)

    candidates = []
    for (row, size), line_words in lines.items():
        top = min(float(w["top"]) for w in line_words)
        if top > page_height * 0.45 or size < median * 1.25:
            continue
        text = " ".join(w["text"] for w in sorted(line_words, key=lambda w: float(w["x0"])))
        candidates.append((size, top, text))
    if not candidates:
        return ""
    best_size = max(c[0] for c in candidates)
    title_lines = sorted((c for c in candidates if abs(c[0] - best_size) < 0.6), key=lambda c: c[1])
    return clean(" ".join(c[2] for c in title_lines[:2]))[:200]


def _parse_with_pypdf(path: Path, max_slides: int) -> tuple[list[Slide], str]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise MalformedDeckError(
                f"{path.name} is password-protected. Remove the password and upload again."
            )
        if len(reader.pages) > max_slides:
            raise DeckTooLargeError(f"{path.name} has {len(reader.pages)} pages; the limit is {max_slides}.")
        slides = []
        for index, page in enumerate(reader.pages, 1):
            try:
                text = clean(page.extract_text() or "")
                warnings = []
            except Exception as exc:
                text, warnings = "", [f"S{index}: text extraction failed ({type(exc).__name__})."]
            first = text.splitlines()[0] if text else ""
            slides.append(Slide(number=index, title=first[:120], text=text, warnings=warnings))
        title = str((reader.metadata or {}).get("/Title") or "") if reader.metadata else ""
        return slides, clean(title)
    except DeckIngestionError:
        raise
    except Exception as exc:
        log.error("PDF %s could not be parsed: %s: %s", path.name, type(exc).__name__, exc)
        raise MalformedDeckError(
            f"{path.name} could not be read as a PDF. The file may be corrupt or truncated."
        ) from exc
