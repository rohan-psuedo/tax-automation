from dataclasses import asdict, dataclass, field
from typing import Any


class ParseError(Exception):
    """The file could not be read. The message is shown to the user."""


@dataclass
class ParsedPage:
    number: int  # 1-based
    width: int  # rendered image size in pixels
    height: int
    has_text: bool


@dataclass
class ParsedSheet:
    name: str
    rows: list[list[str]]
    total_rows: int  # before truncation


@dataclass
class ParsedDocument:
    page_count: int = 0
    text: str = ""
    has_text_layer: bool = False
    pages: list[ParsedPage] = field(default_factory=list)
    sheets: list[ParsedSheet] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def meta(self) -> dict[str, Any]:
        """What gets stored in Document.parsed (text is stored separately)."""
        return {
            "pages": [asdict(p) for p in self.pages],
            "sheets": [asdict(s) for s in self.sheets],
            "warnings": self.warnings,
        }
