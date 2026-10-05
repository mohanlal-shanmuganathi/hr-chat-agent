"""Chat endpoints. Responses stream as Server-Sent Events:

{"type": "thread", "thread_id": ...}
{"type": "tool_call", "tool": ..., "label": ...}      (zero or more)
{"type": "tool_result", "tool": ..., "ok": ...}        (zero or more)
{"type": "result", "status": "completed"|"awaiting_confirmation", "answer": ..., ...}
{"type": "error", "code": ..., "message": ...}        (instead of result, on failure)
"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.service import ChatError, ChatService, EmployeeIdentity
from app.api.security import CsrfProtected, CurrentPrincipal, Principal
from app.core.logging import get_logger

router = APIRouter(prefix="/api/chat", tags=["chat"])
log = get_logger(__name__)

_THREAD_ID = r"^[A-Za-z0-9_-]{1,64}$"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    thread_id: str | None = Field(default=None, pattern=_THREAD_ID)


class ConfirmRequest(BaseModel):
    thread_id: str = Field(pattern=_THREAD_ID)
    approved: bool


def _chat(request: Request) -> ChatService:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Service is starting or degraded")
    if runtime.chat is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Chat is not configured: {runtime.chat_unavailable}",
        )
    chat: ChatService = runtime.chat
    return chat


def _limit(request: Request, principal: Principal) -> None:
    if not request.app.state.rate_limiter.allow(str(principal.employee_id)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many messages; slow down")


def _identity(principal: Principal) -> EmployeeIdentity:
    return EmployeeIdentity(principal.employee_id, principal.name, principal.location)


def _sse(events: AsyncIterator[dict[str, Any]], thread_id: str) -> StreamingResponse:
    async def body() -> AsyncIterator[str]:
        yield _frame({"type": "thread", "thread_id": thread_id})
        try:
            async for event in events:
                yield _frame(event)
        except ChatError as exc:
            yield _frame({"type": "error", "code": exc.code, "message": exc.message})
        except Exception:
            log.exception("chat.stream_failed")
            yield _frame(
                {"type": "error", "code": "internal_error", "message": "Something went wrong."}
            )

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _frame(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str, ensure_ascii=False)}\n\n"


@router.post("", dependencies=[CsrfProtected])
async def send_message(
    body: ChatRequest, request: Request, principal: CurrentPrincipal
) -> StreamingResponse:
    chat = _chat(request)
    _limit(request, principal)
    thread_id = body.thread_id or uuid.uuid4().hex
    rt = request.app.state.runtime
    events = chat.stream_send(
        _identity(principal),
        thread_id,
        body.message,
        rt.today(),
        getattr(request.state, "request_id", None),
    )
    return _sse(events, thread_id)


@router.post("/confirm", dependencies=[CsrfProtected])
async def confirm_action(
    body: ConfirmRequest, request: Request, principal: CurrentPrincipal
) -> StreamingResponse:
    chat = _chat(request)
    _limit(request, principal)
    rt = request.app.state.runtime
    events = chat.stream_resume(
        _identity(principal),
        body.thread_id,
        body.approved,
        rt.today(),
        getattr(request.state, "request_id", None),
    )
    return _sse(events, body.thread_id)


@router.get("/{thread_id}/messages")
async def thread_messages(
    thread_id: str, request: Request, principal: CurrentPrincipal
) -> dict[str, Any]:
    if not thread_id.replace("-", "").replace("_", "").isalnum() or len(thread_id) > 64:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid thread id")
    chat = _chat(request)
    return {"thread_id": thread_id, "messages": await chat.history(_identity(principal), thread_id)}
