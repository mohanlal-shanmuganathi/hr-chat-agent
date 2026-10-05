"""Tool framework: typed specs, identity-bound context, and one guarded executor.

Tools are plain async functions, independent of any agent framework. The agent layer adapts them.

Security model:
- The caller's identity lives in `ToolContext`, built by the server from the authenticated
  session. Tool *input* models never contain an employee identifier and reject unknown fields,
  so the model cannot ask for anyone else's data.
- Every invocation goes through `execute_tool`: argument validation, timeout, error mapping and
  an audit record. Handlers never see unvalidated input.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.logging import get_logger
from app.domain.rules.config import RulesConfig
from app.hr.provider import HRDataProvider
from app.observability.audit import AuditEvent, AuditSink, redact
from app.rag.retriever import PolicyRetriever

log = get_logger(__name__)


class ToolInput(BaseModel):
    """Base for tool arguments supplied by the model."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


@dataclass(frozen=True)
class ToolContext:
    employee_id: uuid.UUID
    request_id: str | None
    today: date
    thread_id: str | None = None


@dataclass(frozen=True)
class ToolDeps:
    hr: HRDataProvider
    retriever: PolicyRetriever
    rules: RulesConfig
    audit: AuditSink
    hr_contact_email: str
    search_top_k: int = 5


class ToolError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


type Handler[I: ToolInput] = Callable[[I, ToolContext, ToolDeps], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec[InputT: ToolInput]:
    name: str
    description: str
    input_model: type[InputT]
    handler: Handler[InputT]
    side_effect: bool = False
    requires_confirmation: bool = False  # agent must pause for explicit user approval
    timeout_s: float | None = None
    free_text_fields: frozenset[str] = field(default_factory=frozenset)


class ToolResult(BaseModel):
    ok: bool
    data: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


async def execute_tool(
    spec: ToolSpec[Any],
    raw_args: dict[str, Any],
    ctx: ToolContext,
    deps: ToolDeps,
    default_timeout_s: float,
) -> ToolResult:
    started = time.perf_counter()
    outcome, result = "error", ToolResult(ok=False)
    try:
        args = spec.input_model.model_validate(raw_args)
        data = await asyncio.wait_for(
            spec.handler(args, ctx, deps), timeout=spec.timeout_s or default_timeout_s
        )
        outcome, result = "ok", ToolResult(ok=True, data=data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}" for e in exc.errors()
        )
        result = ToolResult(
            ok=False,
            error={"code": "invalid_arguments", "message": problems, "retryable": True},
        )
    except ToolError as exc:
        outcome = "denied" if exc.code in {"forbidden", "not_allowed"} else "error"
        result = ToolResult(
            ok=False,
            error={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
        )
    except TimeoutError:
        result = ToolResult(
            ok=False,
            error={"code": "timeout", "message": "The tool took too long.", "retryable": True},
        )
    except Exception as exc:
        log.exception("tool.unhandled_error", tool=spec.name, error_type=type(exc).__name__)
        result = ToolResult(
            ok=False,
            error={
                "code": "internal_error",
                "message": "The tool failed unexpectedly.",
                "retryable": False,
            },
        )

    latency_ms = int((time.perf_counter() - started) * 1000)
    details: dict[str, Any] = {"args": redact(raw_args, spec.free_text_fields)}
    if result.error:
        details["error_code"] = result.error["code"]
    log.info("tool.executed", tool=spec.name, outcome=outcome, latency_ms=latency_ms, **details)
    await deps.audit.record(
        AuditEvent(
            event_type="tool_call",
            name=spec.name,
            outcome=outcome,
            employee_id=ctx.employee_id,
            request_id=ctx.request_id,
            latency_ms=latency_ms,
            details=details,
        )
    )
    return result
