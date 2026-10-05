"""End-to-end ingestion + hybrid retrieval on synthetic PDFs generated in the test."""

from collections.abc import Iterator
from datetime import date

import pymupdf
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import Document, DocumentStatus, Holiday, Location
from app.domain.policy_scope import PolicyScope, PolicyScopeRule
from app.rag.embeddings import HashingEmbedder
from app.rag.ingest import ingest
from app.rag.retriever import PolicyRetriever
from app.rag.sources import SourceDocument
from app.tools.base import ToolContext, ToolDeps, execute_tool
from app.tools.hr_tools import TOOLS_BY_NAME


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


INDIA_ONLY = PolicyScope(
    policies=[
        PolicyScopeRule(
            name="Leave Policy",
            match="leave policy",
            applies_to=[Location.CHENNAI, Location.KARNATAKA],
        )
    ]
)


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

    report = await ingest(source, session_factory, embedder, settings, INDIA_ONLY)
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
        assert new.applies_to == ["chennai", "karnataka"]  # from the policy scope
        kar = await s.scalar(select(Document).where(Document.source_uri == "test://kar.pdf"))
        assert kar and kar.applies_to is None  # scoped by its location instead
        holidays = (await s.scalars(select(Holiday).order_by(Holiday.holiday_date))).all()
        assert [(h.location, h.name) for h in holidays] == [
            (Location.KARNATAKA, "New Year"),
            (Location.KARNATAKA, "Harvest Day"),
        ]

    # Second run with identical content changes nothing.
    again = await ingest(source, session_factory, embedder, settings, INDIA_ONLY)
    assert {r.action for r in again.rows if r.status == "current"} == {"unchanged"}

    retriever = PolicyRetriever(session_factory, embedder)
    hits = await retriever.search("how many days of zebra leave")
    assert hits and "six days" in hits[0].content
    assert all("twelve days" not in h.content for h in hits)  # superseded text is never served
    assert "v1.1" in hits[0].citation
    assert hits[0].applies_to == [Location.CHENNAI, Location.KARNATAKA]

    # Location filter: Karnataka-only content is hidden from a Chennai employee.
    assert not [h for h in await retriever.search("Harvest Day", Location.CHENNAI) if h.location]
    assert any(h.location for h in await retriever.search("Harvest Day", Location.KARNATAKA))

    # Removing a file from the source removes it from the registry.
    del source.files["kar.pdf"]
    final = await ingest(source, session_factory, embedder, settings, INDIA_ONLY)
    assert final.removed == ["kar.pdf"]


async def test_search_flags_policies_that_do_not_cover_the_employee(
    session_factory: async_sessionmaker[AsyncSession], clean: None, deps: ToolDeps
) -> None:
    settings = Settings(embedding_provider="hashing")
    await ingest(
        FakeSource({"new.pdf": NEW_POLICY}),
        session_factory,
        HashingEmbedder(),
        settings,
        INDIA_ONLY,
    )

    async def search_as(email: str) -> dict[str, object]:
        emp_id = await deps.hr.find_employee_id_by_email(email)
        assert emp_id
        ctx = ToolContext(employee_id=emp_id, request_id=None, today=date(2026, 10, 3))
        res = await execute_tool(
            TOOLS_BY_NAME["search_hr_policies"],
            {"query": "how many days of zebra leave"},
            ctx,
            deps,
            default_timeout_s=5,
        )
        assert res.ok and res.data
        return res.data

    # The USA employee still sees the passage, marked as not theirs, with an explicit note.
    emily = await search_as("emily.carter@example.com")
    passages = emily["passages"]
    assert isinstance(passages, list) and passages
    assert all(p["applies_to_you"] is False for p in passages)
    assert passages[0]["applies_to"] == "Chennai (Tamil Nadu), Karnataka (Bengaluru)"
    note = str(emily["applicability_note"])
    assert "USA" in note and "hr@example.com" in note

    priya = await search_as("priya.r@example.com")
    assert isinstance(priya["passages"], list) and priya["passages"]
    assert all(p["applies_to_you"] is True for p in priya["passages"])
    assert "applicability_note" not in priya
