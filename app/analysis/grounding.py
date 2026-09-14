"""Grounding checks for model output against the extracted deck text.

The LLM engine may only call something a FACT when the quote it cites can be
found on a slide. Claims touching the highest-risk subjects — certifications,
patents, freedom to operate, open-source licenses, IP ownership — are removed
entirely when the deck never mentions the subject, unless the sentence is
explicitly about the absence of that evidence.
"""

from __future__ import annotations

import re

from app.ingestion.document import DeckDocument
from app.models.schemas import NOT_PROVIDED
from app.utils.text import normalize_for_match

SENSITIVE_PATTERNS: dict[str, str] = {
    "certification": r"\b(?:soc ?2|iso[/ ]?(?:iec )?27001|hipaa|fedramp|pci[- ]?dss|hitrust|cmmc|stateramp)\b",
    "patent": r"\bpatent(?:s|ed)?\b",
    "freedom_to_operate": r"freedom[- ]to[- ]operate|\bfto\b",
    "open_source_license": r"\b(?:a?gpl|lgpl|copyleft|apache 2\.0|mit licen[sc]e)\b",
    "ip_ownership": r"\b(?:owns|owned by|assigned to|invention assignment|chain of title)\b",
}
_ABSENCE = re.compile(
    r"\b(?:no|not|none|absent|missing|lacks?|lacking|without|unverified|unconfirmed|not provided|does not|did not|"
    r"unclear|unknown|cannot|planned|roadmap|should|verify|confirm|whether|if any)\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _compact(text: str) -> str:
    """Lowercase with spaces, slashes and hyphens removed, so 'SOC-2' and 'soc 2' compare equal."""
    return re.sub(r"[\s/-]+", "", text.lower())


class DeckIndex:
    def __init__(self, deck: DeckDocument) -> None:
        self.slide_count = deck.slide_count
        self.raw = deck.full_text
        self.normalized = {s.number: normalize_for_match(s.full_text) for s in deck.slides}
        self.tokens = {n: set(text.split()) for n, text in self.normalized.items()}
        self.all_normalized = " ".join(self.normalized.values())
        self.compact_raw = _compact(self.raw)

    def valid_slides(self, slides: list[int]) -> list[int]:
        return sorted({s for s in slides if 1 <= s <= self.slide_count})

    def locate(self, quote: str, cited: list[int] | None = None) -> list[int]:
        """Slides containing the quote verbatim (normalised) or with >=85% token overlap."""
        query = normalize_for_match(quote or "")
        if len(query) < 6:
            return []
        hits = [n for n, text in self.normalized.items() if query in text]
        if not hits:
            words = [w for w in query.split() if len(w) >= 3]
            if len(words) >= 4:
                hits = [n for n, toks in self.tokens.items()
                        if sum(1 for w in words if w in toks) / len(words) >= 0.85]
        if not hits:
            return []
        preferred = [n for n in (cited or []) if n in hits]
        return preferred or hits[:3]

    def numbers_supported(self, value: str) -> bool:
        numbers = _NUMBER.findall(value.replace(",", ""))
        haystack = self.all_normalized.replace(",", "")
        return all(re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", haystack) for n in numbers)

    def mentions(self, name: str) -> bool:
        words = [w for w in normalize_for_match(name).split() if len(w) >= 3]
        return bool(words) and all(w in self.all_normalized for w in words[:2])

    def unsupported_subjects(self, text: str) -> list[str]:
        """Sensitive terms asserted in `text` that the deck never mentions.

        Checked term by term: a deck that mentions SOC 2 does not support a claim of ISO 27001.
        """
        if not text or _ABSENCE.search(text):
            return []
        unsupported = []
        for name, pattern in SENSITIVE_PATTERNS.items():
            for match in re.finditer(pattern, text, re.IGNORECASE):
                if _compact(match.group(0)) not in self.compact_raw:
                    unsupported.append(name)
                    break
        return unsupported

    def scrub(self, text: str) -> tuple[str, int]:
        """Drop sentences that assert unsupported sensitive subjects. Returns (text, removed)."""
        if not text:
            return NOT_PROVIDED, 0
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        kept = [s for s in sentences if not self.unsupported_subjects(s)]
        result = " ".join(kept).strip()
        return (result or NOT_PROVIDED), len(sentences) - len(kept)
