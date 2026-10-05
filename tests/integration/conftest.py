"""Integration fixtures: a real PostgreSQL (with pgvector) at TEST_DATABASE_URL.

Locally:  docker compose up -d db
          docker compose exec db createdb -U hr_agent hr_agent_test
          TEST_DATABASE_URL=postgresql+asyncpg://hr_agent:hr_agent@localhost:5432/hr_agent_test
Tests are skipped when TEST_DATABASE_URL is not set.
"""

import os
from collections.abc import AsyncIterator, Iterator
from datetime import date

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Holiday, Location
from app.domain.rules.config import EXAMPLE_RULES, load_rules
from app.hr.db_provider import DbHRDataProvider
from app.hr.seed import SeedSummary, seed
from app.observability.audit import InMemoryAuditSink
from app.rag.embeddings import HashingEmbedder
from app.rag.retriever import PolicyRetriever
from app.tools.base import ToolDeps

SEED_AS_OF = date(2026, 10, 3)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield url


@pytest.fixture
async def session_factory(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def seeded(session_factory: async_sessionmaker[AsyncSession]) -> SeedSummary:
    async with session_factory() as s:
        return await seed(s, SEED_AS_OF)


@pytest.fixture
async def deps(session_factory: async_sessionmaker[AsyncSession], seeded: SeedSummary) -> ToolDeps:
    async with session_factory() as s:
        await s.execute(delete(Holiday))
        s.add_all(
            [
                Holiday(location=Location.CHENNAI, holiday_date=date(2026, 10, 19), name="Fest A"),
                Holiday(location=Location.KARNATAKA, holiday_date=date(2026, 11, 1), name="Fest B"),
            ]
        )
        await s.commit()
    return ToolDeps(
        hr=DbHRDataProvider(session_factory),
        retriever=PolicyRetriever(session_factory, HashingEmbedder()),
        rules=load_rules(EXAMPLE_RULES),
        audit=InMemoryAuditSink(),
        hr_contact_email="hr@example.com",
    )
