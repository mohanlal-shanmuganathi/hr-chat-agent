"""Document metadata: title, version/effective date, location and version grouping.

Policies state their version in different layouts:
- HR policies: a "Document and Version History" table with rows `<n> <version> <date> ...`
- ISMS policies: a cover block with `Revision: 1.0`
- QMS documents: `Revision No 1.0` and `Date dd/mm/yyyy`
When no date can be read from the text, the PDF's own modification date is used and the
document is flagged for review.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from app.db.models import Location
from app.rag.parsing import ParsedDocument

_VERSION_TABLE_MARKER = "document and version history"
_VERSION = r"(\d{1,2}\.\d{1,2})"
_DATE = (
    r"(\d{1,2}[-/][A-Za-z]{3,9}[-/]\d{2,4}"  # 28-Apr-23, 1-Nov-2022, 03-July-2026
    r"|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})"  # 16-09-2022, 04/08/2024
)
_VERSION_ROW = re.compile(rf"{_VERSION}\s+{_DATE}")
_REVISION = re.compile(r"Revision(?:\s*No)?\s*:?\s*(\d{1,2}\.\d{1,2})", re.IGNORECASE)
_HOLIDAY_TITLE = re.compile(r"^(?:(\w+)\s+)?Holiday List\s*-\s*(\d{4})$", re.IGNORECASE)
_TITLE_NOISE = {"internal", "1", "#"}
_PAGE_LABEL = re.compile(r"^page\s+\d+\s+of\s+\d+$", re.IGNORECASE)
_KEY_NOISE = re.compile(r"\b(revised|updated?|new|i2i|ideas2it)\b|\b20\d{2}\b")


class DocumentKind(StrEnum):
    POLICY = "policy"
    HOLIDAY_LIST = "holiday_list"


class DateSource(StrEnum):
    VERSION_TABLE = "version_table"
    DOCUMENT_TEXT = "document_text"
    PDF_METADATA = "pdf_metadata"
    NONE = "none"


@dataclass(frozen=True)
class DocumentMetadata:
    title: str
    policy_key: str
    kind: DocumentKind
    version: str | None
    effective_date: date | None
    date_source: DateSource
    location: Location | None
    holiday_year: int | None = None

    @property
    def needs_review(self) -> bool:
        # Holiday lists are identified by the year in their title, not a version date.
        if self.kind is DocumentKind.HOLIDAY_LIST:
            return False
        return self.date_source in (DateSource.PDF_METADATA, DateSource.NONE)


def parse_date(raw: str) -> date | None:
    raw = raw.strip().replace("/", "-")
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%d-%B-%Y", "%d-%B-%y", "%d-%m-%Y", "%d-%m-%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def extract_version_history(text: str) -> list[tuple[str, date]]:
    """All (version, date) pairs found in version-history rows, oldest first."""
    flat = re.sub(r"\s+", " ", text)
    rows: list[tuple[str, date]] = []
    for version, raw_date in _VERSION_ROW.findall(flat):
        parsed = parse_date(raw_date)
        if parsed is not None:
            rows.append((version, parsed))
    return sorted(set(rows), key=lambda r: (r[1], _version_tuple(r[0])))


def _filename_title(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0]
    return re.sub(r"^I2I_", "", stem).strip()


def extract_title(doc: ParsedDocument, filename: str) -> str:
    lines = [ln for ln in (doc.pages[0].lines if doc.pages else []) if not _PAGE_LABEL.match(ln)]
    for line in lines:
        if _HOLIDAY_TITLE.match(line):
            return line
    for i, line in enumerate(lines):
        if line.lower() == _VERSION_TABLE_MARKER:
            for prev in reversed(lines[:i]):
                if prev.lower() not in _TITLE_NOISE:
                    return prev
    for i, line in enumerate(lines):
        if line.lower().startswith("revision") and i > 0 and lines[0].isupper():
            return lines[0].title()
    return _filename_title(filename)


def detect_location(title: str) -> Location | None:
    lowered = title.lower()
    if "karnataka" in lowered:
        return Location.KARNATAKA
    if re.search(r"\b(usa|us)\b", lowered):
        return Location.USA
    return None


def policy_key_for(title: str, kind: DocumentKind, location: Location | None) -> str:
    """Normalised name that groups versions of the same policy."""
    if kind is DocumentKind.HOLIDAY_LIST:
        return f"holiday list:{(location or Location.CHENNAI).value}"
    key = _KEY_NOISE.sub(" ", title.lower())
    key = re.sub(r"[^a-z0-9]+", " ", key)
    return " ".join(key.split())


def extract_metadata(doc: ParsedDocument, filename: str) -> DocumentMetadata:
    title = extract_title(doc, filename)
    head = doc.text(max_pages=2)

    holiday = _HOLIDAY_TITLE.match(title)
    kind = DocumentKind.HOLIDAY_LIST if holiday else DocumentKind.POLICY
    location = detect_location(title)
    if kind is DocumentKind.HOLIDAY_LIST and location is None:
        location = Location.CHENNAI  # the generic list is the Chennai (head office) calendar

    version: str | None = None
    effective: date | None = None
    source = DateSource.NONE

    history = extract_version_history(head)
    if history:
        version, effective = history[-1]
        source = DateSource.VERSION_TABLE
    else:
        revision = _REVISION.search(head)
        version = revision.group(1) if revision else None
        dated = re.search(rf"\bDate\s+{_DATE}", head)
        if dated and (parsed := parse_date(dated.group(1))):
            effective, source = parsed, DateSource.DOCUMENT_TEXT
        elif doc.pdf_modified:
            effective, source = doc.pdf_modified, DateSource.PDF_METADATA

    return DocumentMetadata(
        title=title,
        policy_key=policy_key_for(title, kind, location),
        kind=kind,
        version=version,
        effective_date=effective,
        date_source=source,
        location=location,
        holiday_year=int(holiday.group(2)) if holiday else None,
    )


def resolve_current(items: list[tuple[str, DocumentMetadata]]) -> dict[str, str | None]:
    """Map each document id to the id that supersedes it (None = current).

    Within a policy_key the newest effective date wins; ties break on version number.
    """
    groups: dict[str, list[tuple[str, DocumentMetadata]]] = {}
    for doc_id, meta in items:
        groups.setdefault(meta.policy_key, []).append((doc_id, meta))

    result: dict[str, str | None] = {}
    for members in groups.values():
        ranked = sorted(
            members,
            key=lambda m: (
                m[1].effective_date or date.min,
                _version_tuple(m[1].version) if m[1].version else (),
            ),
            reverse=True,
        )
        winner = ranked[0][0]
        for doc_id, _ in ranked:
            result[doc_id] = None if doc_id == winner else winner
    return result
