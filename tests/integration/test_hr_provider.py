from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Holiday, Location
from app.hr.db_provider import DbHRDataProvider
from app.hr.seed import SeedSummary


async def _provider_and_id(
    session_factory: async_sessionmaker[AsyncSession], email: str
) -> tuple[DbHRDataProvider, object]:
    provider = DbHRDataProvider(session_factory)
    emp_id = await provider.find_employee_id_by_email(email)
    assert emp_id is not None
    return provider, emp_id


async def test_seed_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession], seeded: SeedSummary
) -> None:
    from app.hr.seed import seed

    async with session_factory() as s:
        again = await seed(s, date(2026, 10, 3), use_private=False)
    assert again == seeded
    assert seeded.employees == 7


async def test_email_lookup_is_case_insensitive(
    session_factory: async_sessionmaker[AsyncSession], seeded: SeedSummary
) -> None:
    provider = DbHRDataProvider(session_factory)
    assert await provider.find_employee_id_by_email("  Priya.R@Example.com ") is not None
    assert await provider.find_employee_id_by_email("nobody@example.com") is None


async def test_profile_includes_manager_and_probation(
    session_factory: async_sessionmaker[AsyncSession], seeded: SeedSummary
) -> None:
    provider = DbHRDataProvider(session_factory)
    priya_id = await provider.find_employee_id_by_email("priya.r@example.com")
    kavya_id = await provider.find_employee_id_by_email("kavya.s@example.com")
    assert priya_id and kavya_id
    priya = await provider.get_profile(priya_id)
    kavya = await provider.get_profile(kavya_id)
    assert priya.manager_name == "Rohan K"
    assert priya.on_probation is False
    assert kavya.on_probation is True


async def test_balances_account_for_carry_forward_usage_and_pending(
    session_factory: async_sessionmaker[AsyncSession], seeded: SeedSummary
) -> None:
    provider = DbHRDataProvider(session_factory)
    priya_id = await provider.find_employee_id_by_email("priya.r@example.com")
    assert priya_id
    balances = {b.leave_type_code: b for b in await provider.get_leave_balances(priya_id, 2026)}

    # Seeded from the example leave types: CL 8/yr (2 per quarter), EL 16/yr (4 per quarter).
    cl = balances["CL"]
    assert (cl.credited, cl.used, cl.pending, cl.available) == (
        Decimal("8.0"),
        Decimal("2.0"),
        Decimal("2.0"),
        Decimal("4.0"),
    )
    el = balances["EL"]
    assert el.available == Decimal("19.0")  # 8 carried + 16 credited - 5 used


async def test_new_joiner_credited_from_joining_quarter(
    session_factory: async_sessionmaker[AsyncSession], seeded: SeedSummary
) -> None:
    provider = DbHRDataProvider(session_factory)
    kavya_id = await provider.find_employee_id_by_email("kavya.s@example.com")
    assert kavya_id
    balances = {b.leave_type_code: b for b in await provider.get_leave_balances(kavya_id, 2026)}
    assert balances["CL"].credited == Decimal("4.0")  # Q3 + Q4, 2 each


async def test_employee_outside_leave_policy_has_no_balances(
    session_factory: async_sessionmaker[AsyncSession], seeded: SeedSummary
) -> None:
    provider = DbHRDataProvider(session_factory)
    emily_id = await provider.find_employee_id_by_email("emily.carter@example.com")
    assert emily_id
    assert await provider.get_leave_balances(emily_id, 2026) == []


async def test_wfh_count_and_holidays(
    session_factory: async_sessionmaker[AsyncSession], seeded: SeedSummary
) -> None:
    provider = DbHRDataProvider(session_factory)
    priya_id = await provider.find_employee_id_by_email("priya.r@example.com")
    assert priya_id
    assert await provider.count_wfh_days(priya_id, date(2026, 9, 1), date(2026, 9, 30)) == 8

    async with session_factory() as s:
        s.add(Holiday(location=Location.CHENNAI, holiday_date=date(2099, 1, 1), name="Test Day"))
        await s.commit()
    holidays = await provider.list_holidays(Location.CHENNAI, date(2099, 1, 1), date(2099, 12, 31))
    assert [h.name for h in holidays] == ["Test Day"]
    assert Location.CHENNAI in await provider.available_holiday_locations()
