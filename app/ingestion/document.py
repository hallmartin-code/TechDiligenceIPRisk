"""In-memory representation of an extracted deck, independent of source format."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.utils.text import clean


@dataclass
class Slide:
    number: int
    title: str = ""
    text: str = ""
    tables: list[list[list[str]]] = field(default_factory=list)
    notes: str = ""
    chart_text: list[str] = field(default_factory=list)
    diagram_text: list[str] = field(default_factory=list)
    image_count: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def table_text(self) -> str:
        rows = []
        for table in self.tables:
            for row in table:
                cells = [c for c in row if c]
                if cells:
                    rows.append(" | ".join(cells))
        return "\n".join(rows)

    @property
    def full_text(self) -> str:
        """Everything machine-readable on the slide, used for detection and grounding."""
        parts = [self.title, self.text, self.table_text, "\n".join(self.diagram_text),
                 "\n".join(self.chart_text), self.notes]
        return clean("\n".join(p for p in parts if p))

    @property
    def is_image_only(self) -> bool:
        return self.image_count > 0 and len(self.full_text) < 20


@dataclass
class DeckDocument:
    filename: str
    file_type: str  # "pdf" | "pptx" | "ppt"
    sha256: str
    slides: list[Slide]
    metadata_title: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def slide_count(self) -> int:
        return len(self.slides)

    @property
    def text_characters(self) -> int:
        return sum(len(s.full_text) for s in self.slides)

    @property
    def full_text(self) -> str:
        return "\n".join(s.full_text for s in self.slides)

    def slide(self, number: int) -> Slide | None:
        if 1 <= number <= len(self.slides):
            return self.slides[number - 1]
        return None

    def image_only_slides(self) -> list[int]:
        return [s.number for s in self.slides if s.is_image_only]
