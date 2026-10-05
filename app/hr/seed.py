"""Seed the mock HR system of record.

Usage:  python -m app.hr.seed [--as-of YYYY-MM-DD]

Idempotent: mock HR tables are cleared and rebuilt on every run. Knowledge tables (documents,
chunks, holidays) are owned by ingestion and are not touched here.

Leave credits follow the quarterly-credit rule configured in the leave-types file: a quarter's
credit is counted if the quarter has started by `as_of`. Mock assumption (not from policy): a new
joiner is credited from the quarter in which they joined.
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.models import (
    Employee,
    EmploymentStatus,
    HrTicket,
    LeaveBalance,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
    Location,
    Role,
    StaffLoan,
    WfhDay,
)
from app.db.session import create_engine, create_session_factory

SEED_DIR = Path("data/seed")
PRIVATE_LEAVE_TYPES = Path("data/private_policies/config/leave_types.yaml")
EXAMPLE_LEAVE_TYPES = SEED_DIR / "leave_types.example.yaml"

log = get_logger(__name__)


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def quarters_credited(year: int, as_of: date, joined: date) -> int:
    count = 0
    for q in range(4):
        q_start = date(year, 3 * q + 1, 1)
        q_end = date(year, 3 * q + 3, 31 if q in (0, 3) else 30)
        if q_start <= as_of and joined <= q_end:
            count += 1
    return count


@dataclass(frozen=True)
class SeedSummary:
    leave_types: int
    employees: int
    balances: int
    leave_requests: int
    wfh_days: int
    leave_types_source: str


def load_leave_types(use_private: bool = True) -> tuple[list[dict[str, Any]], str]:
    """Leave types from the private policy file when present (and allowed), else the example."""
    path = (
        PRIVATE_LEAVE_TYPES if use_private and PRIVATE_LEAVE_TYPES.exists() else EXAMPLE_LEAVE_TYPES
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data["leave_types"], ("private" if path == PRIVATE_LEAVE_TYPES else "example")


async def seed(session: AsyncSession, as_of: date, use_private: bool = True) -> SeedSummary:
    """Rebuild the mock HR tables. Tests pass use_private=False so results never depend on
    whether the private leave-types file exists on the machine (CI has only the example)."""
    leave_type_rows, source = load_leave_types(use_private)
    employees_data = yaml.safe_load((SEED_DIR / "employees.yaml").read_text(encoding="utf-8"))
    year = as_of.year

    for model in (HrTicket, StaffLoan, WfhDay, LeaveRequest, LeaveBalance, Employee, LeaveType):
        await session.execute(delete(model))

    leave_types: dict[str, LeaveType] = {}
    for row in leave_type_rows:
        leave_types[row["code"]] = LeaveType(
            code=row["code"],
            name=row["name"],
            annual_entitlement_days=_dec(row.get("annual")),
            quarterly_credit_days=_dec(row.get("quarterly")),
            carry_forward_max_days=_dec(row.get("carry_forward_max")),
            accumulation_cap_days=_dec(row.get("cap")),
            encashable=bool(row.get("encashable", False)),
            balance_tracked=bool(row.get("balance_tracked", True)),
            policy_reference=row["ref"],
        )
    session.add_all(leave_types.values())
    await session.flush()

    by_code: dict[str, Employee] = {}
    for e in employees_data["employees"]:
        emp = Employee(
            employee_code=e["code"],
            email=e["email"].lower(),
            full_name=e["name"],
            location=Location(e["location"]),
            department=e["department"],
            designation=e["designation"],
            date_of_joining=e["joined"],
            confirmation_date=e.get("confirmed"),
            prior_experience_months=e.get("prior_experience_months", 0),
            employment_status=EmploymentStatus(e.get("status", "active")),
            role=Role(e.get("role", "employee")),
            children_on_record=e.get("children_on_record", 0),
            annual_ctc_inr=e.get("annual_ctc_inr"),
        )
        by_code[emp.employee_code] = emp
    session.add_all(by_code.values())
    await session.flush()

    n_balances = n_requests = n_wfh = 0
    for e in employees_data["employees"]:
        emp = by_code[e["code"]]
        if e.get("manager"):
            emp.manager_id = by_code[e["manager"]].id

        for loan in e.get("loans", []):
            session.add(
                StaffLoan(
                    employee_id=emp.id,
                    amount_inr=loan["amount"],
                    disbursed_on=loan["disbursed"],
                    closed_on=loan.get("closed"),
                    outstanding_inr=loan.get("outstanding", 0),
                )
            )

        for w in e.get("wfh", []):
            session.add(WfhDay(employee_id=emp.id, wfh_date=w))
            n_wfh += 1

        if not e.get("leave_policy_applicable", True):
            continue

        used: dict[str, Decimal] = {}
        for lv in e.get("leave", []):
            status = LeaveRequestStatus(lv["status"])
            days = Decimal(str(lv["days"]))
            session.add(
                LeaveRequest(
                    employee_id=emp.id,
                    leave_type_code=lv["type"],
                    start_date=lv["start"],
                    end_date=lv["end"],
                    days=days,
                    status=status,
                    reason=lv.get("reason"),
                )
            )
            n_requests += 1
            if status == LeaveRequestStatus.APPROVED:
                used[lv["type"]] = used.get(lv["type"], Decimal(0)) + days

        carried: dict[str, Any] = e.get("carried_forward", {})
        quarters = quarters_credited(year, as_of, emp.date_of_joining)
        for code, lt in leave_types.items():
            if not lt.balance_tracked:
                continue
            credited = (lt.quarterly_credit_days or Decimal(0)) * quarters
            carry = _dec(carried.get(code)) or Decimal(0)
            if credited == 0 and carry == 0 and code not in used:
                continue
            session.add(
                LeaveBalance(
                    employee_id=emp.id,
                    leave_type_code=code,
                    year=year,
                    carried_forward=carry,
                    credited=credited,
                    used=used.get(code, Decimal(0)),
                )
            )
            n_balances += 1

    await session.commit()
    return SeedSummary(
        leave_types=len(leave_types),
        employees=len(by_code),
        balances=n_balances,
        leave_requests=n_requests,
        wfh_days=n_wfh,
        leave_types_source=source,
    )


async def _main(as_of: date) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            summary = await seed(session, as_of)
        log.info("seed.completed", as_of=as_of.isoformat(), **summary.__dict__)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the mock HR database")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    asyncio.run(_main(parser.parse_args().as_of))
