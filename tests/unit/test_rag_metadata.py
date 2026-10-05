"""Synthetic fixtures only: no real policy text in the repository."""

from datetime import date

import pytest

from app.db.models import Location
from app.rag.metadata import (
    DateSource,
    DocumentKind,
    extract_metadata,
    extract_version_history,
    parse_date,
    policy_key_for,
    resolve_current,
)
from app.rag.parsing import ParsedDocument, ParsedPage, remove_repeated_lines


def _doc(*pages: list[str], modified: date | None = None) -> ParsedDocument:
    return ParsedDocument(
        pages=[ParsedPage(number=i + 1, lines=p) for i, p in enumerate(pages)],
        pdf_modified=modified,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("28-Apr-23", date(2023, 4, 28)),
        ("1-Nov-2022", date(2022, 11, 1)),
        ("16-09-2022", date(2022, 9, 16)),
        ("04/08/2024", date(2024, 8, 4)),
        ("03-July-2026", date(2026, 7, 3)),
        ("not a date", None),
    ],
)
def test_parse_date(raw: str, expected: date | None) -> None:
    assert parse_date(raw) == expected


def test_version_history_returns_rows_oldest_first() -> None:
    text = (
        "# Version Release Date\n1 1.0 28-Apr-23 Initial\n2 1.1 30-Oct-25 Revision\n3 1.2 09-Jan-26"
    )
    assert extract_version_history(text) == [
        ("1.0", date(2023, 4, 28)),
        ("1.1", date(2025, 10, 30)),
        ("1.2", date(2026, 1, 9)),
    ]


def test_metadata_from_version_table() -> None:
    doc = _doc(
        ["Internal", "Sample Leave Policy", "Document and Version History", "1 1.0 01-Jan-24 Init"],
        ["2 1.1 01-Feb-25 Update", "1. Purpose", "Text."],
    )
    meta = extract_metadata(doc, "file.pdf")
    assert meta.title == "Sample Leave Policy"
    assert (meta.version, meta.effective_date) == ("1.1", date(2025, 2, 1))
    assert meta.date_source is DateSource.VERSION_TABLE
    assert not meta.needs_review


def test_metadata_from_revision_cover_falls_back_to_pdf_date_and_flags_review() -> None:
    doc = _doc(["SAMPLE SECURITY POLICY", "Revision: 1.0", "Issue Status: A"], modified=None)
    meta = extract_metadata(doc, "I2I_Sample Security Policy.pdf")
    assert meta.title == "Sample Security Policy"
    assert meta.version == "1.0"
    assert meta.needs_review


def test_holiday_list_metadata_and_location() -> None:
    karnataka = extract_metadata(_doc(["Karnataka Holiday List - 2030"]), "k.pdf")
    generic = extract_metadata(_doc(["Holiday List - 2030"]), "g.pdf")
    usa = extract_metadata(_doc(["USA Holiday List - 2030"]), "u.pdf")
    assert karnataka.kind is DocumentKind.HOLIDAY_LIST
    assert (karnataka.location, generic.location, usa.location) == (
        Location.KARNATAKA,
        Location.CHENNAI,
        Location.USA,
    )
    assert karnataka.holiday_year == 2030
    assert not generic.needs_review


def test_policy_key_groups_revisions_of_the_same_policy() -> None:
    a = policy_key_for("Revised Leave Policy", DocumentKind.POLICY, None)
    b = policy_key_for("Leave Policy Update 2026", DocumentKind.POLICY, None)
    c = policy_key_for("Staff Loan Policy", DocumentKind.POLICY, None)
    assert a == b == "leave policy"
    assert c != a


def test_resolve_current_prefers_latest_date_then_version() -> None:
    old = extract_metadata(
        _doc(["Leave Policy", "Document and Version History", "1 1.2 09-Jan-26"]), "old.pdf"
    )
    new = extract_metadata(
        _doc(["Leave Policy", "Document and Version History", "1 1.3 02-Mar-26"]), "new.pdf"
    )
    other = extract_metadata(
        _doc(["Loan Policy", "Document and Version History", "1 1.0 01-Jan-20"]), "loan.pdf"
    )
    result = resolve_current([("old", old), ("new", new), ("loan", other)])
    assert result == {"old": "new", "new": None, "loan": None}


def test_repeated_header_lines_are_removed() -> None:
    pages = [["Company Header", f"Body {i}", "Footer"] for i in range(4)]
    cleaned, removed = remove_repeated_lines(pages)
    assert removed == ["Company Header", "Footer"]
    assert cleaned[2] == ["Body 2"]
