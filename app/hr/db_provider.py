"""`HRDataProvider` backed by the local mock HR database."""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.db.models import (
    Employee,
    Holiday,
    HrTicket,
    LeaveBalance,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
    Location,
    StaffLoan,
    WfhDay,
)
from app.domain.hr import (
    EmployeeProfile,
    HolidayView,
    HrTicketView,
    LeaveBalanceView,
    LeaveRequestView,
    LeaveTypeInfo,
    StaffLoanView,
)
from app.hr.provider import EmployeeNotFoundError


def _year_bounds(year: int) -> tuple[date, date]:
    return date(year, 1, 1), date(year, 12, 31)


class DbHRDataProvider:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def find_employee_id_by_email(self, email: str) -> uuid.UUID | None:
        async with self._sessions() as s:
            result = await s.scalar(
                select(Employee.id).where(Employee.email == email.strip().lower())
            )
            return result

    async def get_profile(self, employee_id: uuid.UUID) -> EmployeeProfile:
        async with self._sessions() as s:
            emp = await s.scalar(
                select(Employee)
                .where(Employee.id == employee_id)
                .options(selectinload(Employee.manager))
            )
            if emp is None:
                raise EmployeeNotFoundError(str(employee_id))
            return EmployeeProfile.model_validate(
                {
                    **{c.key: getattr(emp, c.key) for c in Employee.__table__.columns},
                    "manager_name": emp.manager.full_name if emp.manager else None,
                }
            )

    async def list_leave_types(self) -> list[LeaveTypeInfo]:
        async with self._sessions() as s:
            rows = await s.scalars(select(LeaveType).order_by(LeaveType.code))
            return [LeaveTypeInfo.model_validate(r) for r in rows]

    async def get_leave_balances(self, employee_id: uuid.UUID, year: int) -> list[LeaveBalanceView]:
        start, end = _year_bounds(year)
        pending_sq = (
            select(
                LeaveRequest.leave_type_code,
                func.coalesce(func.sum(LeaveRequest.days), 0).label("pending"),
            )
            .where(
                LeaveRequest.employee_id == employee_id,
                LeaveRequest.status == LeaveRequestStatus.PENDING,
                LeaveRequest.start_date.between(start, end),
            )
            .group_by(LeaveRequest.leave_type_code)
            .subquery()
        )
        stmt = (
            select(LeaveBalance, LeaveType.name, pending_sq.c.pending)
            .join(LeaveType, LeaveType.code == LeaveBalance.leave_type_code)
            .outerjoin(pending_sq, pending_sq.c.leave_type_code == LeaveBalance.leave_type_code)
            .where(LeaveBalance.employee_id == employee_id, LeaveBalance.year == year)
            .order_by(LeaveBalance.leave_type_code)
        )
        async with self._sessions() as s:
            rows = (await s.execute(stmt)).all()
        return [
            LeaveBalanceView(
                leave_type_code=b.leave_type_code,
                leave_type_name=name,
                year=b.year,
                carried_forward=b.carried_forward,
                credited=b.credited,
                used=b.used,
                pending=Decimal(pending or 0),
            )
            for b, name, pending in rows
        ]

    async def list_leave_requests(
        self, employee_id: uuid.UUID, year: int
    ) -> list[LeaveRequestView]:
        start, end = _year_bounds(year)
        async with self._sessions() as s:
            rows = await s.scalars(
                select(LeaveRequest)
                .where(
                    LeaveRequest.employee_id == employee_id,
                    LeaveRequest.start_date.between(start, end),
                )
                .order_by(LeaveRequest.start_date)
            )
            return [LeaveRequestView.model_validate(r) for r in rows]

    async def list_holidays(self, location: Location, start: date, end: date) -> list[HolidayView]:
        async with self._sessions() as s:
            rows = await s.scalars(
                select(Holiday)
                .where(Holiday.location == location, Holiday.holiday_date.between(start, end))
                .order_by(Holiday.holiday_date)
            )
            return [HolidayView.model_validate(r) for r in rows]

    async def available_holiday_locations(self) -> list[Location]:
        async with self._sessions() as s:
            rows = await s.scalars(select(Holiday.location).distinct())
            return sorted(rows, key=lambda loc: loc.value)

    async def count_wfh_days(self, employee_id: uuid.UUID, start: date, end: date) -> int:
        async with self._sessions() as s:
            count = await s.scalar(
                select(func.count())
                .select_from(WfhDay)
                .where(WfhDay.employee_id == employee_id, WfhDay.wfh_date.between(start, end))
            )
            return int(count or 0)

    async def list_staff_loans(self, employee_id: uuid.UUID) -> list[StaffLoanView]:
        async with self._sessions() as s:
            rows = await s.scalars(
                select(StaffLoan)
                .where(StaffLoan.employee_id == employee_id)
                .order_by(StaffLoan.disbursed_on)
            )
            return [StaffLoanView.model_validate(r) for r in rows]

    async def find_leave_request_by_key(
        self, employee_id: uuid.UUID, idempotency_key: str
    ) -> LeaveRequestView | None:
        async with self._sessions() as s:
            row = await s.scalar(
                select(LeaveRequest).where(
                    LeaveRequest.employee_id == employee_id,
                    LeaveRequest.idempotency_key == idempotency_key,
                )
            )
            return LeaveRequestView.model_validate(row) if row else None

    async def submit_leave_request(
        self,
        employee_id: uuid.UUID,
        leave_type_code: str,
        start: date,
        end: date,
        days: Decimal,
        reason: str | None,
        idempotency_key: str,
    ) -> tuple[LeaveRequestView, bool]:
        by_key = select(LeaveRequest).where(LeaveRequest.idempotency_key == idempotency_key)
        async with self._sessions() as s:
            existing = await s.scalar(by_key)
            if existing is not None:
                return LeaveRequestView.model_validate(existing), False
            row = LeaveRequest(
                employee_id=employee_id,
                leave_type_code=leave_type_code,
                start_date=start,
                end_date=end,
                days=days,
                status=LeaveRequestStatus.PENDING,
                reason=reason,
                idempotency_key=idempotency_key,
            )
            s.add(row)
            try:
                await s.commit()
            except IntegrityError:  # concurrent duplicate: return the winner
                await s.rollback()
                winner = await s.scalar(by_key)
                if winner is None:
                    raise
                return LeaveRequestView.model_validate(winner), False
            return LeaveRequestView.model_validate(row), True

    async def create_hr_ticket(
        self, employee_id: uuid.UUID, category: str, summary: str, idempotency_key: str
    ) -> tuple[HrTicketView, bool]:
        by_key = select(HrTicket).where(HrTicket.idempotency_key == idempotency_key)
        async with self._sessions() as s:
            existing = await s.scalar(by_key)
            if existing is not None:
                return HrTicketView.model_validate(existing), False
            row = HrTicket(
                employee_id=employee_id,
                category=category,
                summary=summary,
                status="open",
                idempotency_key=idempotency_key,
            )
            s.add(row)
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                winner = await s.scalar(by_key)
                if winner is None:
                    raise
                return HrTicketView.model_validate(winner), False
            return HrTicketView.model_validate(row), True
