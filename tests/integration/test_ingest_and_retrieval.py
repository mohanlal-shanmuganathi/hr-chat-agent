"""End-to-end ingestion + hybrid retrieval on synthetic PDFs generated in the test."""

from collections.abc import Iterator
from datetime import date

import pymupdf
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import Document, DocumentStatus, Holiday, Location
from app.rag.embeddings import HashingEmbedder
from app.rag.ingest import ingest
from app.rag.retriever import PolicyRetriever
from app.rag.sources import SourceDocument


def _pdf(lines: list[str]) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72.0
    for line in lines:
        page.insert_text((72, y), line, fontsize=10)
        y += 14
    data: bytes = doc.tobytes()
    doc.close()
    return data


OLD_POLICY = [
    "Sample Leave Policy",
    "Document and Version History",
    "1 1.0 01-Jan-29 Initial",
    "1. Entitlement",
    "Employees receive twelve days of zebra leave per calendar year.",
]
NEW_POLICY = [
    "Sample Leave Policy",
    "Document and Version History",
    "1 1.0 01-Jan-29 Initial",
    "2 1.1 01-Mar-30 Revised",
    "1. Entitlement",
    "Employees receive six days of zebra leave per calendar year.",
    "2. Bereavement",
    "Three days of bereavement leave are granted for immediate family.",
]
HOLIDAYS = [
    "Karnataka Holiday List - 2030",
    "S.No.     Holiday      Date        Day",
    "1     New Year     01-Jan-2030     Tuesday",
    "2     Harvest Day     15-Jan-2030     Tuesday",
]


class FakeSource:
    def __init__(self, files: dict[str, list[str]]) -> None:
        # Render once: PDF bytes embed timestamps, so re-rendering would look like a new version.
        self.files = {name: _pdf(lines) for name, lines in files.items()}

    def iter_documents(self) -> Iterator[SourceDocument]:
        for name, content in sorted(self.files.items()):
            yield SourceDocument(uri=f"test://{name}", filename=name, content=content)


@pytest.fixture
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as s:
        await s.execute(delete(Holiday))
        await s.execute(delete(Document))
        await s.commit()


async def test_ingest_versions_holidays_idempotency_and_retrieval(
    session_factory: async_sessionmaker[AsyncSession], clean: None
) -> None:
    settings = Settings(embedding_provider="hashing")
    embedder = HashingEmbedder()
    source = FakeSource({"old.pdf": OLD_POLICY, "new.pdf": NEW_POLICY, "kar.pdf": HOLIDAYS})

    report = await ingest(source, session_factory, embedder, settings)
    by_file = {r.filename: r for r in report.rows}
    assert by_file["new.pdf"].status == "current" and by_file["new.pdf"].chunks > 0
    assert by_file["old.pdf"].status == "superseded" and by_file["old.pdf"].chunks == 0
    assert by_file["kar.pdf"].holidays == 2

    async with session_factory() as s:
        old = await s.scalar(select(Document).where(Document.source_uri == "test://old.pdf"))
        new = await s.scalar(select(Document).where(Document.source_uri == "test://new.pdf"))
        assert old and new
        assert old.status is DocumentStatus.SUPERSEDED and old.superseded_by_id == new.id
        assert new.version == "1.1" and new.effective_date == date(2030, 3, 1)
        holidays = (await s.scalars(select(Holiday).order_by(Holiday.holiday_date))).all()
        assert [(h.location, h.name) for h in holidays] == [
            (Location.KARNATAKA, "New Year"),
            (Location.KARNATAKA, "Harvest Day"),
        ]

    # Second run with identical content changes nothing.
    again = await ingest(source, session_factory, embedder, settings)
    assert {r.action for r in again.rows if r.status == "current"} == {"unchanged"}

    retriever = PolicyRetriever(session_factory, embedder)
    hits = await retriever.search("how many days of zebra leave")
    assert hits and "six days" in hits[0].content
    assert all("twelve days" not in h.content for h in hits)  # superseded text is never served
    assert "v1.1" in hits[0].citation

    # Location filter: Karnataka-only content is hidden from a Chennai employee.
    assert not [h for h in await retriever.search("Harvest Day", Location.CHENNAI) if h.location]
    assert any(h.location for h in await retriever.search("Harvest Day", Location.KARNATAKA))

    # Removing a file from the source removes it from the registry.
    del source.files["kar.pdf"]
    final = await ingest(source, session_factory, embedder, settings)
    assert final.removed == ["kar.pdf"]
