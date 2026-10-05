"""Policy ingestion: source folder -> documents registry, chunks + embeddings, holidays.

Usage:  python -m app.rag.ingest

Idempotent. A document is re-chunked only when its content, the embedding model or the
chunker changed, or when it becomes the current version. Superseded versions stay in the
registry (for audit) but have no chunks, so they can never be retrieved or cited. Documents
removed from the source are removed from the database.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, extract, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.models import Document, DocumentChunk, DocumentStatus, Holiday
from app.db.session import create_engine, create_session_factory
from app.rag.chunking import CHUNKER_VERSION, chunk_document
from app.rag.embeddings import Embedder, create_embedder
from app.rag.holidays import parse_holiday_rows
from app.rag.metadata import DocumentKind, DocumentMetadata, extract_metadata, resolve_current
from app.rag.parsing import ParsedDocument, parse_pdf
from app.rag.sources import DocumentSource, LocalFolderSource, SourceDocument

log = get_logger(__name__)


@dataclass
class ReportRow:
    filename: str
    title: str
    version: str | None
    effective_date: str | None
    status: str
    location: str | None
    chunks: int
    holidays: int
    action: str  # indexed | unchanged | registry-only
    needs_review: bool


@dataclass
class IngestReport:
    rows: list[ReportRow] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def render(self) -> str:
        head = (
            f"{'File':44} {'Ver':5} {'Date':10} {'Status':10} {'Loc':9} "
            f"{'Chk':>4} {'Hol':>4} Action"
        )
        lines = [head, "-" * len(head)]
        for r in sorted(self.rows, key=lambda r: (r.status, r.filename)):
            flag = "  (check version date)" if r.needs_review else ""
            lines.append(
                f"{r.filename[:44]:44} {(r.version or '-'):5} {(r.effective_date or '-'):10} "
                f"{r.status:10} {(r.location or 'all'):9} {r.chunks:>4} {r.holidays:>4} "
                f"{r.action}{flag}"
            )
        for name in self.removed:
            lines.append(f"{name[:44]:44} removed (no longer in source)")
        return "\n".join(lines)


@dataclass(frozen=True)
class _Item:
    source: SourceDocument
    parsed: ParsedDocument
    meta: DocumentMetadata


def index_signature(embedder: Embedder) -> str:
    return f"{embedder.model_name}+chunker-{CHUNKER_VERSION}"


async def _index_chunks(
    session: AsyncSession, doc: Document, item: _Item, embedder: Embedder, settings: Settings
) -> int:
    await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    chunks = chunk_document(
        item.parsed,
        item.meta.title,
        target_words=settings.chunk_target_words,
        overlap_words=settings.chunk_overlap_words,
    )
    vectors = embedder.embed_documents([c.content for c in chunks])
    session.add_all(
        DocumentChunk(
            document_id=doc.id,
            chunk_index=c.index,
            section=c.section,
            page_start=c.page_start,
            page_end=c.page_end,
            content=c.content,
            embedding=v,
        )
        for c, v in zip(chunks, vectors, strict=True)
    )
    doc.index_signature = index_signature(embedder)
    return len(chunks)


async def _load_holidays(session: AsyncSession, doc: Document, item: _Item) -> int:
    meta = item.meta
    if meta.kind is not DocumentKind.HOLIDAY_LIST or meta.location is None:
        return 0
    rows = parse_holiday_rows([ln for p in item.parsed.pages for ln in p.lines])
    if meta.holiday_year:
        rows = [r for r in rows if r.holiday_date.year == meta.holiday_year]
    years = {r.holiday_date.year for r in rows} or {meta.holiday_year or 0}
    await session.execute(
        delete(Holiday).where(
            Holiday.location == meta.location,
            extract("year", Holiday.holiday_date).in_(years),
        )
    )
    seen = set()
    for r in rows:
        if r.holiday_date in seen:
            continue
        seen.add(r.holiday_date)
        if not r.weekday_matches:
            log.warning("ingest.holiday_weekday_mismatch", date=r.holiday_date.isoformat())
        session.add(
            Holiday(
                location=meta.location,
                holiday_date=r.holiday_date,
                name=r.name,
                source_document_id=doc.id,
            )
        )
    return len(seen)


async def ingest(
    source: DocumentSource,
    sessions: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    settings: Settings,
) -> IngestReport:
    items: dict[str, _Item] = {}
    for src in source.iter_documents():
        try:
            parsed = parse_pdf(src.content)
        except Exception as exc:  # one unreadable file must not stop the run
            log.error("ingest.parse_failed", file=src.filename, error_type=type(exc).__name__)
            continue
        items[src.uri] = _Item(src, parsed, extract_metadata(parsed, src.filename))

    superseded_by = resolve_current([(uri, it.meta) for uri, it in items.items()])
    report = IngestReport()

    async with sessions() as session:
        existing = {d.source_uri: d for d in await session.scalars(select(Document))}
        for uri, stale in existing.items():
            if uri not in items:
                await session.execute(delete(Holiday).where(Holiday.source_document_id == stale.id))
                await session.delete(stale)
                report.removed.append(uri.split("://", 1)[-1])
        await session.commit()

    ids: dict[str, uuid.UUID] = {}
    for uri, item in items.items():
        is_current = superseded_by[uri] is None
        async with sessions() as session, session.begin():
            doc = await session.scalar(select(Document).where(Document.source_uri == uri))
            changed = doc is None or doc.content_sha256 != item.source.sha256
            if doc is None:
                doc = Document(source_uri=uri)
                session.add(doc)
            meta = item.meta
            doc.content_sha256 = item.source.sha256
            doc.title = meta.title
            doc.policy_key = meta.policy_key
            doc.version = meta.version
            doc.effective_date = meta.effective_date
            doc.location = meta.location
            doc.page_count = item.parsed.page_count
            doc.needs_review = meta.needs_review
            doc.status = DocumentStatus.CURRENT if is_current else DocumentStatus.SUPERSEDED
            await session.flush()
            ids[uri] = doc.id

            chunk_count = await session.scalar(
                select(func.count()).where(DocumentChunk.document_id == doc.id)
            )
            holidays = 0
            if not is_current:
                await session.execute(
                    delete(DocumentChunk).where(DocumentChunk.document_id == doc.id)
                )
                await session.execute(delete(Holiday).where(Holiday.source_document_id == doc.id))
                chunk_count, action = 0, "registry-only"
            elif changed or doc.index_signature != index_signature(embedder) or not chunk_count:
                chunk_count = await _index_chunks(session, doc, item, embedder, settings)
                holidays = await _load_holidays(session, doc, item)
                action = "indexed"
            else:
                holidays = int(
                    await session.scalar(
                        select(func.count()).where(Holiday.source_document_id == doc.id)
                    )
                    or 0
                )
                action = "unchanged"

        report.rows.append(
            ReportRow(
                filename=item.source.filename,
                title=meta.title,
                version=meta.version,
                effective_date=meta.effective_date.isoformat() if meta.effective_date else None,
                status=("current" if is_current else "superseded"),
                location=meta.location.value if meta.location else None,
                chunks=int(chunk_count or 0),
                holidays=holidays,
                action=action,
                needs_review=meta.needs_review,
            )
        )

    async with sessions() as session, session.begin():
        for uri, winner in superseded_by.items():
            await session.execute(
                update(Document)
                .where(Document.id == ids[uri])
                .values(superseded_by_id=ids[winner] if winner else None)
            )
    return report


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    engine = create_engine(settings)
    try:
        report = await ingest(
            LocalFolderSource(Path(settings.policy_dir)),
            create_session_factory(engine),
            create_embedder(settings),
            settings,
        )
        print(report.render())
        log.info(
            "ingest.completed",
            documents=len(report.rows),
            current=sum(r.status == "current" for r in report.rows),
            chunks=sum(r.chunks for r in report.rows),
            removed=len(report.removed),
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
