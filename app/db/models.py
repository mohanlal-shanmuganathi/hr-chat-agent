"""SQLAlchemy ORM models.

Three groups of tables:
- Mock HR system of record (employees, leave, holidays, WFH): stands in for the employee portal
  until an official API is available. Accessed only through `HRDataProvider`.
- Knowledge (policy documents + chunks with embeddings and full-text vectors).
- Operational (audit log, HR tickets).
"""

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Computed,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 384  # BAAI/bge-small-en-v1.5; changing it requires a migration + re-index


class Base(DeclarativeBase):
    pass


def _enum(cls: type[enum.Enum], name: str) -> Enum:
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


class Location(enum.StrEnum):
    CHENNAI = "chennai"
    KARNATAKA = "karnataka"
    USA = "usa"


class EmploymentStatus(enum.StrEnum):
    ACTIVE = "active"
    NOTICE_PERIOD = "notice_period"
    EXITED = "exited"


class Role(enum.StrEnum):
    EMPLOYEE = "employee"
    MANAGER = "manager"
    HR = "hr"


class LeaveRequestStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class DocumentStatus(enum.StrEnum):
    CURRENT = "current"
    SUPERSEDED = "superseded"


# --------------------------------------------------------------------------- mock HR system


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_code: Mapped[str] = mapped_column(String(32), unique=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)  # stored lowercase
    full_name: Mapped[str] = mapped_column(String(200))
    location: Mapped[Location] = mapped_column(_enum(Location, "location"))
    department: Mapped[str] = mapped_column(String(100))
    designation: Mapped[str] = mapped_column(String(100))
    date_of_joining: Mapped[date] = mapped_column(Date)
    confirmation_date: Mapped[date | None] = mapped_column(Date)  # NULL = still on probation
    prior_experience_months: Mapped[int] = mapped_column(Integer, default=0)
    employment_status: Mapped[EmploymentStatus] = mapped_column(
        _enum(EmploymentStatus, "employment_status"), default=EmploymentStatus.ACTIVE
    )
    role: Mapped[Role] = mapped_column(_enum(Role, "role"), default=Role.EMPLOYEE)
    manager_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employees.id"))
    children_on_record: Mapped[int] = mapped_column(Integer, default=0)
    annual_ctc_inr: Mapped[int | None] = mapped_column(Integer)  # mock; used by loan rules only
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    manager: Mapped["Employee | None"] = relationship(remote_side=[id])


class LeaveType(Base):
    """Leave categories and their rules, transcribed from the current leave policy."""

    __tablename__ = "leave_types"

    code: Mapped[str] = mapped_column(String(8), primary_key=True)  # CL, SL, EL, PL, ML, PTL
    name: Mapped[str] = mapped_column(String(100))
    annual_entitlement_days: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    quarterly_credit_days: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    carry_forward_max_days: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    accumulation_cap_days: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    encashable: Mapped[bool] = mapped_column(default=False)
    balance_tracked: Mapped[bool] = mapped_column(default=True)  # False for event-based leave
    policy_reference: Mapped[str] = mapped_column(String(200))


class LeaveBalance(Base):
    __tablename__ = "leave_balances"
    __table_args__ = (UniqueConstraint("employee_id", "leave_type_code", "year"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    leave_type_code: Mapped[str] = mapped_column(ForeignKey("leave_types.code"))
    year: Mapped[int] = mapped_column(Integer)
    carried_forward: Mapped[Decimal] = mapped_column(Numeric(5, 1), default=Decimal(0))
    credited: Mapped[Decimal] = mapped_column(Numeric(5, 1), default=Decimal(0))
    used: Mapped[Decimal] = mapped_column(Numeric(5, 1), default=Decimal(0))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LeaveRequest(Base):
    __tablename__ = "leave_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    leave_type_code: Mapped[str] = mapped_column(ForeignKey("leave_types.code"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    days: Mapped[Decimal] = mapped_column(Numeric(5, 1))
    status: Mapped[LeaveRequestStatus] = mapped_column(
        _enum(LeaveRequestStatus, "leave_request_status"), default=LeaveRequestStatus.PENDING
    )
    reason: Mapped[str | None] = mapped_column(String(500))
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Holiday(Base):
    __tablename__ = "holidays"
    __table_args__ = (UniqueConstraint("location", "holiday_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location: Mapped[Location] = mapped_column(_enum(Location, "location"))
    holiday_date: Mapped[date] = mapped_column(Date, index=True)
    name: Mapped[str] = mapped_column(String(200))
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id"))


class StaffLoan(Base):
    __tablename__ = "staff_loans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    amount_inr: Mapped[int] = mapped_column(Integer)
    disbursed_on: Mapped[date] = mapped_column(Date)
    closed_on: Mapped[date | None] = mapped_column(Date)  # NULL = still being repaid
    outstanding_inr: Mapped[int] = mapped_column(Integer, default=0)


class WfhDay(Base):
    __tablename__ = "wfh_days"
    __table_args__ = (UniqueConstraint("employee_id", "wfh_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    wfh_date: Mapped[date] = mapped_column(Date)


# --------------------------------------------------------------------------- knowledge


class Document(Base):
    """One source policy file. Only `current` documents are used for answers."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_uri: Mapped[str] = mapped_column(String(1000), unique=True)
    content_sha256: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(300))
    policy_key: Mapped[str] = mapped_column(String(300), index=True)  # groups versions
    version: Mapped[str | None] = mapped_column(String(32))
    effective_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[DocumentStatus] = mapped_column(
        _enum(DocumentStatus, "document_status"), default=DocumentStatus.CURRENT
    )
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id"))
    location: Mapped[Location | None] = mapped_column(_enum(Location, "location"))
    page_count: Mapped[int] = mapped_column(Integer)
    needs_review: Mapped[bool] = mapped_column(default=False)  # version date not found in text
    # "<embedding model>+chunker-<n>": a change to either forces re-indexing
    index_signature: Mapped[str | None] = mapped_column(String(100))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(300))
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', content)", persisted=True)
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))


# --------------------------------------------------------------------------- operational


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    event_type: Mapped[str] = mapped_column(String(64))  # tool_call, approval, auth, ...
    name: Mapped[str] = mapped_column(String(100))
    outcome: Mapped[str] = mapped_column(String(32))  # ok, denied, error
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # redacted, no PII


class HrTicket(Base):
    __tablename__ = "hr_tickets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    category: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(32), default="open")
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
