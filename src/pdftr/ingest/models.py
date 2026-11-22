"""Data types shared by the ingest steps."""

from __future__ import annotations

from dataclasses import dataclass, field

BBox = tuple[float, float, float, float]  # x0, y0, x1, y1 in points, top-left origin

# Roles that become embeddable chunks (others are skipped or used as context).
TEXT_ROLES = {"paragraph", "list_item", "callout", "caption", "footnote"}


@dataclass
class Span:
    text: str
    bbox: BBox
    font: str
    size: float
    color: int
    bold: bool
    mono: bool


@dataclass
class Line:
    bbox: BBox
    spans: list[Span]

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)


@dataclass
class DoclingItem:
    """One layout element found by Docling (one region on one page)."""

    page_no: int
    label: str  # Docling DocItemLabel value
    layer: str  # body | furniture
    bbox: BBox
    text: str
    order: int  # reading order across the whole run
    marker: str | None = None
    enumerated: bool | None = None
    table_md: str | None = None
    table_cells: list[dict] | None = None
    code_language: str | None = None
    multi_prov: bool = False


@dataclass
class Block:
    """One piece of text on one page: the unit that is redrawn in Thai."""

    page_no: int
    seq: int  # position on the page in reading order
    role: str
    docling_label: str
    bbox: BBox
    markup: str  # text with <c>code</c>, <k>key term</k>, <b>bold</b>
    text: str  # plain text
    level: int | None = None  # heading level or list depth
    marker: str | None = None
    table_md: str | None = None
    style: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    translate: bool = True
    db_id: int | None = None

    @property
    def ord(self) -> int:
        return self.page_no * 1000 + self.seq


@dataclass
class Chunk:
    """One unit for embedding. Can span several blocks and pages."""

    kind: str
    blocks: list[Block]
    text: str
    section_path: list[str]
    markup: str = ""  # same text with <c>/<k>/<b> tags (what M3 translates)
    context: str = ""
    list_level: int | None = None
    part: int = 0
    embed: bool = True
    flags: list[str] = field(default_factory=list)

    @property
    def ord(self) -> int:
        return self.blocks[0].ord

    @property
    def page_start(self) -> int:
        return min(b.page_no for b in self.blocks)

    @property
    def page_end(self) -> int:
        return max(b.page_no for b in self.blocks)


@dataclass
class TermCandidate:
    norm: str
    term: str  # most frequent surface form
    kind: str  # term | acronym | code
    sources: set[str] = field(default_factory=set)
    freq: int = 0
    first_page: int | None = None
    example: str | None = None
