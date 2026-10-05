"""Liveness and readiness probes."""

import asyncio

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from app.core.logging import get_logger
from app.db.session import ping

router = APIRouter(tags=["health"])
log = get_logger(__name__)

_READY_TIMEOUT_S = 3.0


class HealthStatus(BaseModel):
    status: str
    checks: dict[str, str] = {}


@router.get("/health", response_model=HealthStatus)
async def health() -> HealthStatus:
    """Liveness: the process is up. Never touches dependencies."""
    return HealthStatus(status="ok")


@router.get("/ready", response_model=HealthStatus)
async def ready(request: Request, response: Response) -> HealthStatus:
    """Readiness: dependencies are reachable."""
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        # Startup failed; say why instead of blaming the database.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        reason = getattr(request.app.state, "startup_error", None) or "not initialised"
        return HealthStatus(status="unavailable", checks={"startup": f"failed: {reason}"})
    try:
        await asyncio.wait_for(ping(runtime.engine), timeout=_READY_TIMEOUT_S)
        checks = {"database": "ok", "chat": "ok" if runtime.chat else "not_configured"}
    except Exception as exc:
        log.warning("ready.database_unavailable", error_type=type(exc).__name__)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthStatus(status="unavailable", checks={"database": "unavailable"})
    return HealthStatus(status="ok", checks=checks)
