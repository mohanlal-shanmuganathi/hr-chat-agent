"""PDF text extraction with page numbers and header/footer removal."""

from collections import Counter
from dataclasses import dataclass, field
from datetime import date

import pymupdf

_MIN_PAGES_FOR_BOILERPLATE = 3
_BOILERPLATE_PAGE_RATIO = 0.6


@dataclass(frozen=True)
class ParsedPage:
    number: int  # 1-based
    lines: list[str]


@dataclass(frozen=True)
class ParsedDocument:
    pages: list[ParsedPage]
    pdf_modified: date | None = None
    pdf_title: str | None = None
    removed_boilerplate: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def text(self, max_pages: int | None = None) -> str:
        pages = self.pages if max_pages is None else self.pages[:max_pages]
        return "\n".join(line for p in pages for line in p.lines)


def _pdf_date(raw: str | None) -> date | None:
    # PDF dates look like "D:20260304115055+05'30'"
    if not raw or len(raw) < 10:
        return None
    digits = raw.removeprefix("D:")[:8]
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


def remove_repeated_lines(pages: list[list[str]]) -> tuple[list[list[str]], list[str]]:
    """Drop lines that repeat on most pages (running headers, footers, page labels)."""
    if len(pages) < _MIN_PAGES_FOR_BOILERPLATE:
        return pages, []
    counts = Counter(line for lines in pages for line in set(lines))
    threshold = max(2, int(len(pages) * _BOILERPLATE_PAGE_RATIO))
    boilerplate = {line for line, n in counts.items() if n >= threshold}
    return [[ln for ln in lines if ln not in boilerplate] for lines in pages], sorted(boilerplate)


def parse_pdf(content: bytes) -> ParsedDocument:
    with pymupdf.open(stream=content, filetype="pdf") as doc:  # type: ignore[no-untyped-call]
        raw_pages = [
            [ln.strip() for ln in page.get_text("text", sort=True).splitlines() if ln.strip()]
            for page in doc
        ]
        metadata = doc.metadata or {}
    cleaned, removed = remove_repeated_lines(raw_pages)
    return ParsedDocument(
        pages=[ParsedPage(number=i + 1, lines=lines) for i, lines in enumerate(cleaned)],
        pdf_modified=_pdf_date(metadata.get("modDate") or metadata.get("creationDate")),
        pdf_title=(metadata.get("title") or None),
        removed_boilerplate=removed,
    )
