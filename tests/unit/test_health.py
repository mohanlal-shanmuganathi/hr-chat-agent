from httpx import AsyncClient


async def test_health_is_ok_without_dependencies(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_response_carries_request_id(client: AsyncClient) -> None:
    resp = await client.get("/health", headers={"X-Request-ID": "abc123"})
    assert resp.headers["X-Request-ID"] == "abc123"


async def test_oversized_request_id_is_replaced(client: AsyncClient) -> None:
    resp = await client.get("/health", headers={"X-Request-ID": "x" * 500})
    assert resp.headers["X-Request-ID"] != "x" * 500
    assert len(resp.headers["X-Request-ID"]) == 32


async def test_ready_reports_unavailable_when_db_down(client: AsyncClient) -> None:
    resp = await client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unavailable"
    # The real startup failure is reported (here: the database refused the connection).
    assert body["checks"]["startup"].startswith("failed: ")


async def test_unknown_route_uses_error_envelope(client: AsyncClient) -> None:
    resp = await client.get("/nope", headers={"X-Request-ID": "r1"})
    assert resp.status_code == 404
    assert resp.json() == {
        "error": {"code": "http_404", "message": "Not Found", "request_id": "r1"}
    }
