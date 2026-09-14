"""Sentence-level context heuristics: negation and future / roadmap language.

These guard the two reliability rules most often broken by keyword matching:
"no GPL components" must not read as copyleft exposure, and "SOC 2 planned for
2027" must not read as an attestation.
"""

from __future__ import annotations

import re
from datetime import date

_NEGATION = re.compile(
    r"\b(?:no|not|never|without|zero|none|nor|free of|lacks?|lacking|avoid(?:s|ed|ing)?|"
    r"eliminat(?:e|ed|es|ing)|replac(?:e|ed|es|ing)|transitioned (?:away )?from|"
    r"migrat(?:e|ed|ing) (?:off|away from)|previously|formerly|instead of|rather than|"
    r"prohibit(?:s|ed)?|excludes?|excluding)\b\W+(?:[\w/-]+\W+){0,3}$",
    re.IGNORECASE,
)

_FUTURE = re.compile(
    r"\b(?:plans?|planned|planning|plan to|roadmap|will|upcoming|in progress|underway|"
    r"pursuing|pursue|targeting|targeted|expected|expects?|anticipated?|intends?|intended|"
    r"working towards?|preparing(?: for)?|readiness|coming soon|pending|forecast(?:ed)?|"
    r"projected|projections?|next (?:\d+|six|twelve|eighteen|12|18|24) months|"
    r"by (?:q[1-4]|end of|eoy|year[- ]end)|post[- ]raise|after (?:this|the) (?:round|raise)|"
    r"(?:to|will) (?:hire|build|obtain|achieve|complete|file|pursue|launch))\b",
    re.IGNORECASE,
)

_FUTURE_TITLE = re.compile(
    r"\b(?:roadmap|milestones?|use of (?:funds|proceeds)|next steps|timeline|projections?|"
    r"forecast|growth plan|the ask|financial plan|plan)\b",
    re.IGNORECASE,
)

_YEAR = re.compile(r"\b(20\d\d)\b")


def is_negated(unit: str, start: int) -> bool:
    """True when a negation word sits within three words before position `start`."""
    window = unit[max(0, start - 60):start]
    return bool(_NEGATION.search(window))


def has_future_year(unit: str, today: date | None = None) -> bool:
    year = (today or date.today()).year
    return any(int(match) > year for match in _YEAR.findall(unit))


def is_future_context(unit: str, title: str = "", today: date | None = None) -> bool:
    """True when the sentence (or its slide title) describes plans rather than facts."""
    return bool(_FUTURE.search(unit)) or has_future_year(unit, today) or bool(
        title and _FUTURE_TITLE.search(title)
    )


def is_projection(unit: str, today: date | None = None) -> bool:
    """Narrower test used for financial metrics: explicit forecasts or future years."""
    return bool(
        re.search(r"\b(?:projected|projections?|forecast(?:ed)?|target(?:ed)?|plan(?:ned)?|"
                  r"expected|pro forma|by 20\d\d|budget)\b", unit, re.IGNORECASE)
    ) or has_future_year(unit, today)
