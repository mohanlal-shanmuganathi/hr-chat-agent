"""Consistent error envelope for every API error response."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from app.core.logging import get_logger

log = get_logger(__name__)


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: list[dict[str, object]] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _respond(
    request: Request,
    status: int,
    code: str,
    message: str,
    details: list[dict[str, object]] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code, message=message, request_id=_request_id(request), details=details
        )
    )
    return JSONResponse(status_code=status, content=body.model_dump(exclude_none=True))


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return _respond(request, exc.status_code, f"http_{exc.status_code}", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
        return _respond(request, 422, "validation_error", "Invalid request", details)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        # Never leak internals to the client; full detail is in the logs under the request_id.
        log.exception("api.unhandled_exception", error_type=type(exc).__name__)
        return _respond(request, 500, "internal_error", "An unexpected error occurred")
