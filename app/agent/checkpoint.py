"""Conversation-state persistence (LangGraph checkpointer) in the same PostgreSQL database."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.config import Settings


def _psycopg_url(sqlalchemy_url: str) -> str:
    # SQLAlchemy uses "postgresql+asyncpg://"; psycopg wants plain "postgresql://".
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@asynccontextmanager
async def postgres_checkpointer(settings: Settings) -> AsyncIterator[AsyncPostgresSaver]:
    url = _psycopg_url(settings.database_url.get_secret_value())
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()  # idempotent: creates/upgrades checkpoint tables
        yield saver
