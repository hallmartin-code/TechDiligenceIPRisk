"""Pitch-deck ingestion: PDF, PPTX and legacy PPT."""

from app.ingestion.errors import DeckIngestionError
from app.ingestion.extractor import extract_company_snapshot, load_deck

__all__ = ["DeckIngestionError", "extract_company_snapshot", "load_deck"]
