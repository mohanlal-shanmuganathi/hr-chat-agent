"""Hybrid policy retrieval: Postgres full-text + pgvector cosine, fused with Reciprocal Rank Fusion.

Full-text catches exact terms ("EL", "paternity", "iAssistant"); vectors catch paraphrases
("leave when my baby is born"). Only chunks of *current* documents are searchable (superseded
versions have no chunks). A location filter keeps location-specific documents (e.g. a Karnataka
holiday list) out of answers for other locations; documents without a location apply to everyone.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Location
from app.rag.embeddings import Embedder

_RRF_K = 60
_CANDIDATES = 20

# Full-text query uses OR semantics: plainto_tsquery() ANDs every word, which makes natural
# questions miss; rewriting '&' to '|' lets ts_rank_cd reward chunks matching more terms.
_HYBRID_SQL = text(
    """
    WITH q AS (
        SELECT to_tsquery('english',
                 replace(plainto_tsquery('english', :query)::text, '&', '|')) AS tsq
    ),
    eligible AS (
        SELECT c.id, c.tsv, c.embedding
        FROM document_chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE d.status = 'current'
          AND (d.location IS NULL OR CAST(:location AS location) IS NULL
               OR d.location = CAST(:location AS location))
    ),
    fts AS (
        SELECT e.id, row_number() OVER (ORDER BY ts_rank_cd(e.tsv, q.tsq) DESC) AS rnk
        FROM eligible e, q
        WHERE q.tsq IS NOT NULL AND e.tsv @@ q.tsq
        ORDER BY ts_rank_cd(e.tsv, q.tsq) DESC
        LIMIT :candidates
    ),
    vec AS (
        SELECT e.id,
               row_number() OVER (ORDER BY e.embedding <=> CAST(:embedding AS vector)) AS rnk
        FROM eligible e
        ORDER BY e.embedding <=> CAST(:embedding AS vector)
        LIMIT :candidates
    ),
    fused AS (
        SELECT id, sum(1.0 / (:rrf_k + rnk)) AS score,
               bool_or(src = 'fts') AS keyword_hit
        FROM (SELECT id, rnk, 'fts' AS src FROM fts
              UNION ALL SELECT id, rnk, 'vec' AS src FROM vec) u
        GROUP BY id
    )
    SELECT c.id AS chunk_id, d.id AS document_id, d.title, d.version, d.effective_date,
           d.location, c.section, c.page_start, c.page_end, c.content,
           f.score, f.keyword_hit
    FROM fused f
    JOIN document_chunks c ON c.id = f.id
    JOIN documents d ON d.id = c.document_id
    ORDER BY f.score DESC
    LIMIT :top_k
    """
)


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: int
    document_id: uuid.UUID
    title: str
    version: str | None
    effective_date: date | None
    location: Location | None
    section: str | None
    page_start: int
    page_end: int
    content: str
    score: float
    keyword_hit: bool

    @property
    def citation(self) -> str:
        pages = (
            f"p. {self.page_start}"
            if self.page_start == self.page_end
            else f"pp. {self.page_start}-{self.page_end}"
        )
        version = f" v{self.version}" if self.version else ""
        section = f", {_short_section(self.section)}" if self.section else ""
        return f"{self.title}{version}{section} ({pages})"


def _short_section(section: str, limit: int = 70) -> str:
    """A chunk may span several headings ("4.1 Casual Leave; 4.2 Sick Leave"): cite the first."""
    first = section.split("; ")[0].strip()
    more = " ff." if "; " in section else ""
    return (first[: limit - 1] + "…" if len(first) > limit else first) + more


class PolicyRetriever:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], embedder: Embedder) -> None:
        self._sessions = sessions
        self._embedder = embedder

    async def search(
        self, query: str, location: Location | None = None, top_k: int = 5
    ) -> list[RetrievedChunk]:
        query = query.strip()
        if not query:
            return []
        # CPU-bound (ONNX inference, first call also loads the model): keep the event loop free.
        vector = await asyncio.to_thread(self._embedder.embed_query, query)
        params = {
            "query": query,
            "location": location.value if location else None,
            "embedding": "[" + ",".join(f"{v:.6f}" for v in vector) + "]",
            "candidates": _CANDIDATES,
            "rrf_k": _RRF_K,
            "top_k": top_k,
        }
        async with self._sessions() as session:
            rows = (await session.execute(_HYBRID_SQL, params)).mappings().all()
        return [
            RetrievedChunk(
                chunk_id=r["chunk_id"],
                document_id=r["document_id"],
                title=r["title"],
                version=r["version"],
                effective_date=r["effective_date"],
                location=Location(r["location"]) if r["location"] else None,
                section=r["section"],
                page_start=r["page_start"],
                page_end=r["page_end"],
                content=r["content"],
                score=float(r["score"]),
                keyword_hit=bool(r["keyword_hit"]),
            )
            for r in rows
        ]
