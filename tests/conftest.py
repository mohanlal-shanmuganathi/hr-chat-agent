from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    # Points at a port where nothing listens, so /ready deterministically reports unavailable.
    return Settings(
        environment="test",
        log_json=False,
        database_url="postgresql+asyncpg://nobody:nobody@127.0.0.1:1/none",  # type: ignore[arg-type]
        db_connect_timeout_s=0.5,
        embedding_provider="hashing",
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
