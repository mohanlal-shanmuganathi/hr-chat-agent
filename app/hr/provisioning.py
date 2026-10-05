"""Mock-HR only: create a demo HR profile for a real SSO user on first login.

The mock HR database has only fictional employees. So a real Ideas2IT user can try the agent
with "their" data, the template employee's profile attributes and balances are copied to a new
record keyed by their email. A real employee-portal integration never does this; it is enabled
by MOCK_HR_AUTOPROVISION and off by default.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Employee, LeaveBalance, StaffLoan


class ProvisioningError(Exception):
    pass


async def provision_demo_employee(
    sessions: async_sessionmaker[AsyncSession], email: str, name: str, template_email: str
) -> uuid.UUID:
    email = email.strip().lower()
    async with sessions() as s:
        existing = await s.scalar(select(Employee.id).where(Employee.email == email))
        if existing:
            return existing
        template = await s.scalar(select(Employee).where(Employee.email == template_email))
        if template is None:
            raise ProvisioningError("Template employee not found; run the seed first.")
        emp = Employee(
            employee_code=f"DEMO-{uuid.uuid4().hex[:6].upper()}",
            email=email,
            full_name=name,
            location=template.location,
            department=template.department,
            designation=template.designation,
            date_of_joining=template.date_of_joining,
            confirmation_date=template.confirmation_date,
            prior_experience_months=template.prior_experience_months,
            employment_status=template.employment_status,
            role=template.role,
            manager_id=template.manager_id,
            children_on_record=template.children_on_record,
            annual_ctc_inr=template.annual_ctc_inr,
        )
        s.add(emp)
        await s.flush()
        for bal in await s.scalars(
            select(LeaveBalance).where(LeaveBalance.employee_id == template.id)
        ):
            s.add(
                LeaveBalance(
                    employee_id=emp.id,
                    leave_type_code=bal.leave_type_code,
                    year=bal.year,
                    carried_forward=bal.carried_forward,
                    credited=bal.credited,
                    used=bal.used,
                )
            )
        for loan in await s.scalars(select(StaffLoan).where(StaffLoan.employee_id == template.id)):
            s.add(
                StaffLoan(
                    employee_id=emp.id,
                    amount_inr=loan.amount_inr,
                    disbursed_on=loan.disbursed_on,
                    closed_on=loan.closed_on,
                    outstanding_inr=loan.outstanding_inr,
                )
            )
        await s.commit()
        return emp.id
