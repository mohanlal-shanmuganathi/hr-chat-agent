"""HTTP API: authentication, CSRF, streaming chat, confirmations, Google SSO, isolation."""

import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.google_oidc import GoogleIdentity, OIDCError
from app.config import Settings
from app.db.models import Employee
from app.hr.seed import SeedSummary
from app.main import create_app
from tests.fakes import ScriptedChatModel, call, say

CSRF = {"X-Requested-With": "hr-chat"}


class FakeGoogle:
    def __init__(self) -> None:
        self.identity: GoogleIdentity | OIDCError = GoogleIdentity(
            "new.person@ideas2it.com", "New Person", "ideas2it.com"
        )
        self.nonces: list[str] = []

    async def exchange_and_verify(self, code: str, nonce: str) -> GoogleIdentity:
        self.nonces.append(nonce)
        if isinstance(self.identity, OIDCError):
            raise self.identity
        return self.identity


def make_settings(database_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "log_json": False,
        "database_url": database_url,
        "embedding_provider": "hashing",
        "session_secret": "test-secret-" + "x" * 40,
        "auth_dev_login_enabled": True,
        "google_client_id": "client-id",
        "google_client_secret": "client-secret",
        "mock_hr_autoprovision": True,
        "rate_limit_per_minute": 50,
        "scope_check_enabled": False,  # scripted models here script only the agent's calls
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def model() -> ScriptedChatModel:
    return ScriptedChatModel(script=[])


@pytest.fixture
async def client(
    database_url: str, seeded: SeedSummary, model: ScriptedChatModel, google: FakeGoogle
) -> AsyncIterator[AsyncClient]:
    app = create_app(make_settings(database_url), model=model, google_verifier=google)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c


async def login(client: AsyncClient, email: str = "priya.r@example.com") -> None:
    resp = await client.post("/auth/dev-login", json={"email": email}, headers=CSRF)
    assert resp.status_code == 200, resp.text


def events(body: str) -> list[dict[str, Any]]:
    return [json.loads(line[6:]) for line in body.split("\n\n") if line.startswith("data: ")]


async def test_requires_authentication(client: AsyncClient) -> None:
    assert (await client.get("/auth/me")).status_code == 401
    resp = await client.post("/api/chat", json={"message": "hi"}, headers=CSRF)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "http_401"


async def test_dev_login_requires_csrf_header_and_demo_user(client: AsyncClient) -> None:
    no_csrf = await client.post("/auth/dev-login", json={"email": "priya.r@example.com"})
    assert no_csrf.status_code == 403
    real = await client.post("/auth/dev-login", json={"email": "ceo@ideas2it.com"}, headers=CSRF)
    assert real.status_code == 403

    await login(client)
    me = (await client.get("/auth/me")).json()
    assert me == {
        "name": "Priya R",
        "email": "priya.r@example.com",
        "location": "Chennai (Tamil Nadu)",
        "auth_method": "dev",
    }
    cookie = client.cookies.get("hr_session")
    assert cookie and cookie.count(".") == 2  # signed JWT


async def test_auth_config_lists_demo_users(client: AsyncClient) -> None:
    config = (await client.get("/auth/config")).json()
    assert config["google_enabled"] and config["dev_login_enabled"]
    assert len(config["demo_users"]) == 7


async def test_streaming_chat_turn(client: AsyncClient, model: ScriptedChatModel) -> None:
    model.script.extend([call("get_my_leave_balances"), say("You have 2 days of CL.")])
    await login(client)
    resp = await client.post("/api/chat", json={"message": "my CL?"}, headers=CSRF)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    evs = events(resp.text)
    assert [e["type"] for e in evs] == ["thread", "tool_call", "tool_result", "result"]
    assert evs[1]["label"] == "Checking your leave balances"
    assert evs[-1]["answer"] == "You have 2 days of CL." and evs[-1]["status"] == "completed"

    history = (await client.get(f"/api/chat/{evs[0]['thread_id']}/messages")).json()
    assert [m["role"] for m in history["messages"]] == ["user", "assistant"]


async def test_confirmation_flow_over_api(client: AsyncClient, model: ScriptedChatModel) -> None:
    model.script.extend(
        [
            call("submit_leave_request", leave_type="EL", start="2026-11-09", end="2026-11-10"),
            say("Submitted for approval."),
        ]
    )
    await login(client)
    first = events(
        (await client.post("/api/chat", json={"message": "apply EL"}, headers=CSRF)).text
    )
    result = first[-1]
    assert result["status"] == "awaiting_confirmation"
    thread = first[0]["thread_id"]

    blocked = events(
        (
            await client.post(
                "/api/chat", json={"message": "hello", "thread_id": thread}, headers=CSRF
            )
        ).text
    )
    assert blocked[-1] == {
        "type": "error",
        "code": "confirmation_pending",
        "message": "Please approve or reject the pending action before sending a new message.",
    }

    done = events(
        (
            await client.post(
                "/api/chat/confirm", json={"thread_id": thread, "approved": True}, headers=CSRF
            )
        ).text
    )
    assert done[-1]["answer"] == "Submitted for approval."
    assert done[-1]["tools_used"][0]["ok"] is True


async def test_threads_are_private_over_api(client: AsyncClient, model: ScriptedChatModel) -> None:
    model.script.append(say("Hi Priya"))
    await login(client)
    thread = events(
        (await client.post("/api/chat", json={"message": "secret question"}, headers=CSRF)).text
    )[0]["thread_id"]

    await login(client, "rohan.k@example.com")
    other = (await client.get(f"/api/chat/{thread}/messages")).json()
    assert other["messages"] == []


async def test_google_login_flow_with_autoprovision(
    client: AsyncClient, google: FakeGoogle, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    start = await client.get("/auth/google/login")
    assert start.status_code == 302
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["hd"] == ["ideas2it.com"] and query["scope"] == ["openid email profile"]
    state, nonce = query["state"][0], query["nonce"][0]

    bad = await client.get("/auth/google/callback", params={"code": "c", "state": "forged"})
    assert bad.headers["location"].startswith("/?login_error")

    start = await client.get("/auth/google/login")
    query = parse_qs(urlparse(start.headers["location"]).query)
    state, nonce = query["state"][0], query["nonce"][0]
    ok = await client.get("/auth/google/callback", params={"code": "c", "state": state})
    assert ok.status_code == 302 and ok.headers["location"] == "/"
    assert google.nonces[-1] == nonce
    me = (await client.get("/auth/me")).json()
    assert me["email"] == "new.person@ideas2it.com" and me["auth_method"] == "google"
    async with session_factory() as s:
        emp = await s.scalar(select(Employee).where(Employee.email == "new.person@ideas2it.com"))
        assert emp and emp.employee_code.startswith("DEMO-")


async def test_google_rejects_other_domains(client: AsyncClient, google: FakeGoogle) -> None:
    google.identity = OIDCError("account is not in the ideas2it.com workspace")
    start = await client.get("/auth/google/login")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    resp = await client.get("/auth/google/callback", params={"code": "c", "state": state})
    assert resp.headers["location"].startswith("/?login_error")
    assert (await client.get("/auth/me")).status_code == 401


async def test_security_headers_and_ui(client: AsyncClient) -> None:
    resp = await client.get("/")
    assert resp.status_code == 200 and "HR Assistant" in resp.text
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"] == "no-cache"
    script = await client.get("/static/app.js")
    assert script.status_code == 200 and script.headers["cache-control"] == "no-cache"
    assert "cache-control" not in (await client.get("/health")).headers


async def test_rate_limit(database_url: str, seeded: SeedSummary) -> None:
    model = ScriptedChatModel(script=[say("a"), say("b"), say("c")])
    app = create_app(make_settings(database_url, rate_limit_per_minute=2), model=model)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await login(c)
            codes = [
                (await c.post("/api/chat", json={"message": "hi"}, headers=CSRF)).status_code
                for _ in range(3)
            ]
    assert codes == [200, 200, 429]


async def test_chat_reports_missing_llm_configuration(
    database_url: str, seeded: SeedSummary
) -> None:
    app = create_app(make_settings(database_url, google_api_key=None))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await login(c)
            resp = await c.post("/api/chat", json={"message": "hi"}, headers=CSRF)
            ready = (await c.get("/ready")).json()
    assert resp.status_code == 503 and "GOOGLE_API_KEY" in resp.json()["error"]["message"]
    assert ready["checks"]["chat"] == "not_configured"
