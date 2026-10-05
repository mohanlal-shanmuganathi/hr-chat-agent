"""Port for the HR system of record.

The agent's tools depend only on this protocol. Today it is backed by a mock database
(`DbHRDataProvider`); an official employee-portal (iAssistant) API client can implement the same
protocol later without touching tools or the agent.

Every method is scoped to a single employee ID. Callers must pass the *authenticated* employee's
ID; this layer never decides who the caller is.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Protocol

from app.db.models import Location
from app.domain.hr import (
    EmployeeProfile,
    HolidayView,
    HrTicketView,
    LeaveBalanceView,
    LeaveRequestView,
    LeaveTypeInfo,
    StaffLoanView,
)


class HRDataError(Exception):
    """Base error for HR data access."""


class EmployeeNotFoundError(HRDataError):
    pass


class HRDataProvider(Protocol):
    async def find_employee_id_by_email(self, email: str) -> uuid.UUID | None: ...

    async def get_profile(self, employee_id: uuid.UUID) -> EmployeeProfile: ...

    async def list_leave_types(self) -> list[LeaveTypeInfo]: ...

    async def get_leave_balances(
        self, employee_id: uuid.UUID, year: int
    ) -> list[LeaveBalanceView]: ...

    async def list_leave_requests(
        self, employee_id: uuid.UUID, year: int
    ) -> list[LeaveRequestView]: ...

    async def list_holidays(
        self, location: Location, start: date, end: date
    ) -> list[HolidayView]: ...

    async def available_holiday_locations(self) -> list[Location]: ...

    async def count_wfh_days(self, employee_id: uuid.UUID, start: date, end: date) -> int: ...

    async def list_staff_loans(self, employee_id: uuid.UUID) -> list[StaffLoanView]: ...

    # Writes. Both are idempotent on `idempotency_key`: repeating a call returns the original
    # record instead of creating a duplicate.
    async def find_leave_request_by_key(
        self, employee_id: uuid.UUID, idempotency_key: str
    ) -> LeaveRequestView | None: ...

    async def submit_leave_request(
        self,
        employee_id: uuid.UUID,
        leave_type_code: str,
        start: date,
        end: date,
        days: Decimal,
        reason: str | None,
        idempotency_key: str,
    ) -> tuple[LeaveRequestView, bool]: ...  # (request, created)

    async def create_hr_ticket(
        self, employee_id: uuid.UUID, category: str, summary: str, idempotency_key: str
    ) -> tuple[HrTicketView, bool]: ...  # (ticket, created)
