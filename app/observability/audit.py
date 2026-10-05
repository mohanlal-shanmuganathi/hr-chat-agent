"""Audit trail for tool calls and approvals.

Audit records must never become a second copy of sensitive conversation content: free-text
arguments (e.g. a ticket summary) are reduced to their length before being stored.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models import AuditLog

log = get_logger(__name__)

_FREE_TEXT_LIMIT = 60


@dataclass(frozen=True)
class AuditEvent:
    event_type: str  # tool_call | approval | auth
    name: str
    outcome: str  # ok | error | denied | rejected
    employee_id: uuid.UUID | None
    request_id: str | None
    latency_ms: int | None = None
    details: dict[str, Any] = field(default_factory=dict)


def redact(args: dict[str, Any], free_text_fields: frozenset[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in args.items():
        if key in free_text_fields and isinstance(value, str):
            out[key] = f"<redacted {len(value)} chars>"
        elif isinstance(value, str) and len(value) > _FREE_TEXT_LIMIT:
            out[key] = value[:_FREE_TEXT_LIMIT] + "…"
        else:
            out[key] = value if isinstance(value, int | float | bool | type(None)) else str(value)
    return out


class AuditSink(Protocol):
    async def record(self, event: AuditEvent) -> None: ...


class DbAuditSink:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(self, event: AuditEvent) -> None:
        try:
            async with self._sessions() as s:
                s.add(
                    AuditLog(
                        request_id=event.request_id,
                        employee_id=event.employee_id,
                        event_type=event.event_type,
                        name=event.name,
                        outcome=event.outcome,
                        latency_ms=event.latency_ms,
                        details=event.details,
                    )
                )
                await s.commit()
        except Exception as exc:
            # Auditing must not break the user's request; the structured log still has the event.
            log.error("audit.write_failed", name=event.name, error_type=type(exc).__name__)


class InMemoryAuditSink:
    """For tests."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)
