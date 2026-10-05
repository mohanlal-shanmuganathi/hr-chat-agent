"""Holiday parsing, chunking and the hashing embedder (synthetic fixtures only)."""

from datetime import date

from app.rag.chunking import chunk_document, is_heading
from app.rag.embeddings import HashingEmbedder
from app.rag.holidays import parse_holiday_rows
from app.rag.parsing import ParsedDocument, ParsedPage


def test_holiday_rows_including_wrapped_name_and_weekday_check() -> None:
    lines = [
        "Sample Holiday List - 2030",
        "S.No.        Holiday         Date          Day",
        "1      New Year       01-Jan-2030     Tuesday",
        "Founders Day, observed",
        "2      on Friday      05-July-2030    Friday",
        "3      Wrong Day      02-Jan-2030     Monday",
        "Note: weekends are off.",
    ]
    rows = parse_holiday_rows(lines)
    assert [(r.holiday_date, r.name) for r in rows] == [
        (date(2030, 1, 1), "New Year"),
        (date(2030, 7, 5), "Founders Day, observed on Friday"),
        (date(2030, 1, 2), "Wrong Day"),
    ]
    assert [r.weekday_matches for r in rows] == [True, True, False]


def test_heading_detection() -> None:
    assert is_heading("4.3 Earned Leave (EL)")
    assert is_heading("ELIGIBILITY CRITERIA")
    assert is_heading("Purpose:")
    assert not is_heading("Employees are entitled to six days of leave.")
    assert not is_heading("1      New Year       01-Jan-2030     Tuesday")  # table row


def _doc(*pages: list[str]) -> ParsedDocument:
    return ParsedDocument(pages=[ParsedPage(number=i + 1, lines=p) for i, p in enumerate(pages)])


def test_chunks_follow_sections_and_skip_version_table() -> None:
    filler = " ".join(["word"] * 120)
    doc = _doc(
        ["Sample Policy", "Document and Version History", "1 1.0 01-Jan-24 Initial", "1. Purpose"],
        [filler, "2. Eligibility", filler],
    )
    chunks = chunk_document(doc, "Sample Policy", target_words=200, overlap_words=10)
    assert [c.section for c in chunks] == ["1. Purpose", "2. Eligibility"]
    assert all(c.content.startswith("Sample Policy — ") for c in chunks)
    assert "Initial" not in chunks[0].content
    assert (chunks[0].page_start, chunks[1].page_end) == (1, 2)


def test_version_table_ends_at_its_last_row_before_a_plain_heading() -> None:
    # Regression: "Objective" is not a recognised heading, and the whole body used to be dropped.
    doc = _doc(
        [
            "Sample Outing Policy",
            "Document and Version History",
            "#   Version  Release Date Description of Change   Approved by",
            "1      1.0     27-03-2023               Initial             HR Lead",
            "2      1.1     20-06-2024               Revised budget      HR Lead",
            "Objective",
            "To promote team building and improve employee morale across all teams.",
            "Budget",
            "The budget for team events is set per employee for every financial year.",
        ]
    )
    text = "\n".join(c.content for c in chunk_document(doc, "Sample Outing Policy"))
    assert "Objective" in text and "per employee for every financial year" in text
    assert "27-03-2023" not in text and "Revised budget" not in text


def test_wrapped_table_cells_after_the_last_row_are_dropped() -> None:
    doc = _doc(
        [
            "Sample Leave Policy",
            "Document and Version History",
            "#   Version    Release     Description of Change    Approved by",
            "1      1.0      28-Apr-23                  Initial           HR Lead",
            "Updated Sick Leave and",
            "2      1.1     02-Mar-26                        HR Lead",
            "Increased EL count",
            "1.  Purpose",
            "This policy sets out the leave types available to every employee.",
        ]
    )
    text = "\n".join(c.content for c in chunk_document(doc, "Sample Leave Policy"))
    assert "Increased EL count" not in text and "Updated Sick Leave" not in text
    assert "1.  Purpose" in text and "leave types available" in text


def test_long_section_is_split_with_overlap_and_keeps_label() -> None:
    long_text = [" ".join(f"w{i}_{j}" for j in range(50)) for i in range(10)]
    chunks = chunk_document(_doc(["1. Rules", *long_text]), "P", target_words=150, overlap_words=40)
    assert len(chunks) > 2
    assert all("1. Rules" in (c.section or "") for c in chunks)
    first_tail = chunks[0].content.split("\n")[-1]
    assert first_tail in chunks[1].content  # overlap carries context across the split


def test_hashing_embedder_is_deterministic_normalised_and_lexically_sensible() -> None:
    emb = HashingEmbedder(dim=384)
    a, b, c = emb.embed_documents(["sick leave per year", "yearly sick leave", "laptop password"])
    assert a == emb.embed_query("sick leave per year")
    assert abs(sum(v * v for v in a) - 1.0) < 1e-9

    def cos(x: list[float], y: list[float]) -> float:
        return sum(p * q for p, q in zip(x, y, strict=True))

    assert cos(a, b) > cos(a, c)
