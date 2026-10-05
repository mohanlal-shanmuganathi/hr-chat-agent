"""Section-aware chunking.

Text is split at headings so a chunk rarely mixes two topics. Long sections are split further at
roughly `target_words`, with a small word overlap inside the same section. Every chunk carries its
document title and section in a header line so it reads on its own (for retrieval and for the LLM).
"""

import re
from dataclasses import dataclass

from app.rag.parsing import ParsedDocument

_NUMBERED = re.compile(r"^(\d{1,2}(\.\d{1,2})*\.?|[IVX]{1,4}\.|[a-z]\))\s+[A-Z(].{0,80}$")
_LABEL = re.compile(r"^[A-Z][A-Za-z&/,()' -]{2,60}:$")
_VERSION_TABLE = "document and version history"
_MAX_HEADING_WORDS = 10
_TABLE_GAP = re.compile(r"\S\s{3,}\S")  # wide gaps = table columns in layout text
# A version-history row: an index or version number, then a release date (27-03-2023, 28-Apr-23).
_DATED_ROW = re.compile(
    r"^\s*\d{1,2}(\.\d{1,2})?\s.*\b\d{1,2}[-/.]([A-Za-z]{3,9}|\d{1,2})[-/.]\d{2,4}\b"
)
_TABLE_MAX_LINES = 40  # the version table never spans more than this
_FRAGMENT_LOOKAHEAD = 4  # wrapped description lines that may trail the last row
_PROSE_WORDS = 9  # a line this long is body text, not a wrapped table cell

# Bump when chunking rules change so ingestion re-chunks existing documents.
CHUNKER_VERSION = 3


def is_heading(line: str) -> bool:
    words = line.split()
    if not words or len(words) > _MAX_HEADING_WORDS or line.endswith((".", ",", ";")):
        return False
    if _TABLE_GAP.search(line):
        return False
    if _NUMBERED.match(line) or _LABEL.match(line):
        return True
    letters = [c for c in line if c.isalpha()]
    return len(letters) >= 4 and all(c.isupper() for c in letters) and len(words) <= 8


@dataclass(frozen=True)
class Chunk:
    index: int
    section: str | None
    page_start: int
    page_end: int
    content: str  # includes the "title — section" header line


def _starts_with_heading(buf: list[tuple[int, str]]) -> bool:
    return bool(buf) and is_heading(buf[0][1])


def _version_table_end(lines: list[tuple[int, str]], start: int) -> int:
    """Index of the first line after the version-history table that begins at `start`.

    The table ends after its last dated row. Short wrapped cells after that row ("Increased EL
    count") are dropped only when a heading follows them; otherwise they are the start of the body
    (plain headings such as "Objective" are not recognised by `is_heading`).
    """
    stop = min(len(lines), start + _TABLE_MAX_LINES)
    last_row = None
    for k in range(start, stop):
        line = lines[k][1]
        if is_heading(line) and not re.match(r"^\d{1,2}(\.\d)?$", line):
            stop = k
            break
        if _DATED_ROW.match(line):
            last_row = k
    if last_row is None:
        return stop if stop < start + _TABLE_MAX_LINES else start  # no rows found: drop nothing
    for k in range(last_row + 1, min(len(lines), last_row + 1 + _FRAGMENT_LOOKAHEAD)):
        line = lines[k][1]
        if is_heading(line):
            return k
        if len(line.split()) >= _PROSE_WORDS:
            break
    return last_row + 1


def _drop_version_history(doc: ParsedDocument) -> list[tuple[int, str]]:
    """Flatten to (page, line), without the "Document and Version History" table."""
    lines = [(page.number, line) for page in doc.pages for line in page.lines]
    for i, (_, line) in enumerate(lines):
        if line.lower() == _VERSION_TABLE:
            return lines[:i] + lines[_version_table_end(lines, i + 1) :]
    return lines


def chunk_document(
    doc: ParsedDocument, title: str, target_words: int = 320, overlap_words: int = 40
) -> list[Chunk]:
    chunks: list[Chunk] = []
    carried_section: str | None = None  # section a chunk continues from, if it starts mid-section
    buf: list[tuple[int, str]] = []

    def words_in(lines: list[tuple[int, str]]) -> int:
        return sum(len(ln.split()) for _, ln in lines)

    def flush(keep_overlap: bool) -> None:
        nonlocal buf, carried_section
        if not any(not is_heading(ln) for _, ln in buf):
            return  # headings only so far; keep them for the next chunk
        headings = [ln.rstrip(":") for _, ln in buf if is_heading(ln)]
        labels = [carried_section] if carried_section and not _starts_with_heading(buf) else []
        labels += headings
        label = "; ".join(dict.fromkeys(labels))[:300] or None
        header = f"{title} — {label}" if label else title
        body = [ln for _, ln in buf]
        chunks.append(
            Chunk(
                index=len(chunks),
                section=label,
                page_start=buf[0][0],
                page_end=buf[-1][0],
                content=header + "\n" + "\n".join(body),
            )
        )
        carried_section = headings[-1] if headings else carried_section
        if keep_overlap:
            tail: list[tuple[int, str]] = []
            for item in reversed(buf):
                if words_in(tail) >= overlap_words:
                    break
                tail.insert(0, item)
            buf = tail
        else:
            buf = []

    for page_no, line in _drop_version_history(doc):
        # Start a new chunk at a heading unless the current one is still tiny.
        if is_heading(line) and words_in(buf) >= target_words // 4:
            flush(keep_overlap=False)
        buf.append((page_no, line))
        if words_in(buf) >= target_words:
            flush(keep_overlap=True)
    flush(keep_overlap=False)
    return chunks
