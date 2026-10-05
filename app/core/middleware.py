"""Request-context middleware: assigns a request ID, binds it to logs, logs access lines."""

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger

REQUEST_ID_HEADER = "X-Request-ID"
_MAX_INBOUND_ID_LEN = 64

log = get_logger(__name__)


def _safe_request_id(inbound: str | None) -> str:
    # Accept a caller-supplied ID only if it is short and printable; never trust it blindly.
    if inbound and len(inbound) <= _MAX_INBOUND_ID_LEN and inbound.isprintable():
        return inbound
    return uuid.uuid4().hex


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = _safe_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request.unhandled_error", path=request.url.path)
            raise
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        response.headers[REQUEST_ID_HEADER] = request_id
        log.info(
            "request.completed",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
        )
        return response
