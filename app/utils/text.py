"""Small text helpers used across ingestion, analysis and reporting."""

from __future__ import annotations

import re
import unicodedata

_PUNCT_MAP = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "•": " ",
        " ": " ",
        "…": "...",
    }
)
_WS = re.compile(r"[ \t\f\v]+")
_SENTENCE_BOUNDARY = re.compile(r"([.!?;])\s+(?=[A-Z0-9\"'(])")
_ABBREVIATIONS = re.compile(
    r"(?:\b(?:dr|mr|mrs|ms|prof|inc|corp|co|ltd|llc|jr|sr|st|vs|no|approx|est|u\.s|e\.g|i\.e|etc)"
    r"|\b[A-Z])\.$",
    re.IGNORECASE,
)


def clean(text: str | None) -> str:
    """Normalise unicode punctuation and collapse runs of spaces (keeps newlines)."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).translate(_PUNCT_MAP)
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def normalize_for_match(text: str) -> str:
    """Lowercase, strip punctuation to spaces: used to verify quotes against slides."""
    text = clean(text).lower()
    text = re.sub(r"[^a-z0-9$%.]+", " ", text)
    text = re.sub(r"(?<![0-9])\.|\.(?![0-9])", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_CONTINUATION_END = re.compile(
    r"(?:[,(/&-]|\b(?:and|or|the|a|an|of|to|for|with|on|in|by|at|from|our|its|their|is|are|was|were|as|into|than))$",
    re.IGNORECASE,
)


def _join_wrapped_lines(text: str) -> list[str]:
    """Re-join lines that PDF extraction broke mid-sentence, so phrases spanning a wrap still match."""
    lines: list[str] = []
    for line in clean(text).splitlines():
        if lines and not re.search(r"[.!?;:]$", lines[-1]) and (
                line[:1].islower() or _CONTINUATION_END.search(lines[-1])):
            lines[-1] = f"{lines[-1]} {line}"
        else:
            lines.append(line)
    return lines


def split_units(text: str) -> list[str]:
    """Split slide text into sentence-sized units for signal detection."""
    units: list[str] = []
    for line in _join_wrapped_lines(text):
        start = 0
        for match in _SENTENCE_BOUNDARY.finditer(line):
            end = match.start() + 1
            if match.group(1) == "." and _ABBREVIATIONS.search(line[max(0, end - 8):end]):
                continue
            part = line[start:end].strip()
            if part:
                units.append(part)
            start = match.end()
        tail = line[start:].strip()
        if tail:
            units.append(tail)
    return units


def shorten(text: str, limit: int) -> str:
    """Trim to `limit` characters at a word boundary, marking the cut with an ellipsis."""
    text = " ".join((text or "").split())
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[: max(limit - 1, 1)]
    if " " in cut[int(limit * 0.6):]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "…"


def lower_first(text: str) -> str:
    """Lowercase the first letter for mid-sentence use, keeping acronyms and brand casing (IP, FedRAMP)."""
    if not text:
        return text
    first_word = text.split(" ", 1)[0]
    if len(first_word) > 1 and any(ch.isupper() for ch in first_word[1:]):
        return text
    return text[0].lower() + text[1:]


def slide_ref(slides: list[int], limit: int = 4) -> str:
    if not slides:
        return ""
    shown = ", ".join(f"S{s}" for s in slides[:limit])
    return shown + ("…" if len(slides) > limit else "")


def slugify(value: str, max_length: int = 60) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return (value[:max_length].strip("_") or "company").lower()


def join_nonempty(parts: list[str], sep: str = "; ") -> str:
    return sep.join(p for p in parts if p)
