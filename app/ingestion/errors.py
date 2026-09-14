"""Ingestion error types. Messages are written for the person who uploaded the file."""

from __future__ import annotations


class DeckIngestionError(Exception):
    """The deck could not be read. `str(error)` is safe to show to a user."""


class UnsupportedDeckError(DeckIngestionError):
    """The file type is not PDF / PPT / PPTX, or needs a converter that is missing."""


class MalformedDeckError(DeckIngestionError):
    """The file claims a supported type but its contents are corrupt or unreadable."""


class DeckTooLargeError(DeckIngestionError):
    """The file exceeds the configured size or slide limits."""
