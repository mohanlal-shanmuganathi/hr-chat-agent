"""HR domain read models. Plain data passed from the HR data provider to tools and services."""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, computed_field

from app.db.models import EmploymentStatus, LeaveRequestStatus, Location, Role


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)


class EmployeeProfile(_Frozen):
    id: uuid.UUID
    employee_code: str
    email: str
    full_name: str
    location: Location
    department: str
    designation: str
    date_of_joining: date
    confirmation_date: date | None
    prior_experience_months: int
    employment_status: EmploymentStatus
    role: Role
    manager_name: str | None = None
    children_on_record: int
    annual_ctc_inr: int | None = None
    leave_policy_applicable: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def on_probation(self) -> bool:
        return self.confirmation_date is None


class LeaveTypeInfo(_Frozen):
    code: str
    name: str
    annual_entitlement_days: Decimal | None
    quarterly_credit_days: Decimal | None
    carry_forward_max_days: Decimal | None
    accumulation_cap_days: Decimal | None
    encashable: bool
    balance_tracked: bool
    policy_reference: str


class LeaveBalanceView(_Frozen):
    leave_type_code: str
    leave_type_name: str
    year: int
    carried_forward: Decimal
    credited: Decimal
    used: Decimal
    pending: Decimal

    @computed_field  # type: ignore[prop-decorator]
    @property
    def available(self) -> Decimal:
        return self.carried_forward + self.credited - self.used - self.pending


class LeaveRequestView(_Frozen):
    id: uuid.UUID
    leave_type_code: str
    start_date: date
    end_date: date
    days: Decimal
    status: LeaveRequestStatus
    reason: str | None


class HolidayView(_Frozen):
    holiday_date: date
    name: str
    location: Location


class HrTicketView(_Frozen):
    id: uuid.UUID
    category: str
    summary: str
    status: str


class StaffLoanView(_Frozen):
    amount_inr: int
    disbursed_on: date
    closed_on: date | None
    outstanding_inr: int
