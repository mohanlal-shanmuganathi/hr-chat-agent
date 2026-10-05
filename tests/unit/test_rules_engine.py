"""Rules engine tests. Use the public example rules (placeholder values), never private policy."""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.db.models import EmploymentStatus, Location, Role
from app.domain.hr import EmployeeProfile, HolidayView, LeaveBalanceView, LeaveTypeInfo
from app.domain.rules.calendar import InvalidDateRangeError, count_working_days
from app.domain.rules.config import EXAMPLE_RULES, RulesConfig, load_rules
from app.domain.rules.leave import LeaveRequestFacts, check_leave_eligibility
from app.domain.rules.location import parse_location, resolve_location
from app.domain.rules.results import Verdict
from app.domain.rules.wfh import check_wfh_eligibility, total_experience_months

TODAY = date(2030, 3, 4)  # a Monday


@pytest.fixture(scope="module")
def rules() -> RulesConfig:
    return load_rules(EXAMPLE_RULES)


def profile(**overrides: object) -> EmployeeProfile:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "employee_code": "E1",
        "email": "e1@example.com",
        "full_name": "Test Person",
        "location": Location.CHENNAI,
        "department": "Eng",
        "designation": "Engineer",
        "date_of_joining": date(2025, 1, 6),
        "confirmation_date": date(2025, 7, 6),
        "prior_experience_months": 12,
        "employment_status": EmploymentStatus.ACTIVE,
        "role": Role.EMPLOYEE,
        "children_on_record": 0,
    }
    base.update(overrides)
    return EmployeeProfile.model_validate(base)


LEAVE_TYPES = [
    LeaveTypeInfo(
        code=c,
        name=n,
        annual_entitlement_days=None,
        quarterly_credit_days=None,
        carry_forward_max_days=None,
        accumulation_cap_days=None,
        encashable=False,
        balance_tracked=tracked,
        policy_reference=f"Example §{c}",
    )
    for c, n, tracked in [
        ("CL", "Casual Leave", True),
        ("SL", "Sick Leave", True),
        ("EL", "Earned Leave", True),
        ("ML", "Maternity Leave", False),
        ("PTL", "Paternity Leave", False),
    ]
]


def balance(code: str, credited: str, used: str = "0", pending: str = "0") -> LeaveBalanceView:
    return LeaveBalanceView(
        leave_type_code=code,
        leave_type_name={"CL": "Casual Leave", "SL": "Sick Leave", "EL": "Earned Leave"}[code],
        year=2030,
        carried_forward=Decimal(0),
        credited=Decimal(credited),
        used=Decimal(used),
        pending=Decimal(pending),
    )


HOLIDAYS = [
    HolidayView(holiday_date=date(2030, 3, 6), name="Test Festival", location=Location.CHENNAI)
]
BALANCES = [balance("CL", "3", used="1"), balance("SL", "3"), balance("EL", "6", pending="1")]


def check(rules: RulesConfig, prof: EmployeeProfile | None = None, **facts: object):  # type: ignore[no-untyped-def]
    request = LeaveRequestFacts(**facts)  # type: ignore[arg-type]
    return check_leave_eligibility(
        prof or profile(), request, LEAVE_TYPES, BALANCES, HOLIDAYS, rules, TODAY
    )


# ----------------------------------------------------------------------------- calendar


def test_working_days_exclude_weekends_and_holidays(rules: RulesConfig) -> None:
    # Mon 4 Mar .. Sun 10 Mar 2030, with a holiday on Wed 6 Mar
    wd = count_working_days(date(2030, 3, 4), date(2030, 3, 10), HOLIDAYS, rules.calendar)
    assert (wd.calendar_days, wd.working_days) == (7, Decimal(4))
    assert [e.reason for e in wd.excluded] == ["Test Festival", "weekend", "weekend"]


def test_half_day_and_invalid_ranges(rules: RulesConfig) -> None:
    half = count_working_days(date(2030, 3, 4), date(2030, 3, 4), [], rules.calendar, True)
    assert half.working_days == Decimal("0.5")
    with pytest.raises(InvalidDateRangeError):
        count_working_days(date(2030, 3, 5), date(2030, 3, 4), [], rules.calendar)
    with pytest.raises(InvalidDateRangeError):
        count_working_days(date(2030, 3, 4), date(2030, 3, 5), [], rules.calendar, True)


# ----------------------------------------------------------------------------- leave


def test_eligible_casual_leave_with_holiday_inside(rules: RulesConfig) -> None:
    res = check(rules, leave_type_code="cl", start=date(2030, 3, 5), end=date(2030, 3, 6)).result
    assert res.verdict is Verdict.ELIGIBLE
    assert (res.requested_days, res.available_days) == (Decimal(1), Decimal(2))
    assert res.next_steps  # how to apply


def test_insufficient_balance_suggests_alternatives(rules: RulesConfig) -> None:
    res = check(rules, leave_type_code="CL", start=date(2030, 3, 11), end=date(2030, 3, 13)).result
    assert res.verdict is Verdict.NOT_ELIGIBLE
    assert "insufficient_balance" in {f.code for f in res.findings}
    assert "Earned Leave" in res.next_steps[0]


def test_notice_period_blocks_leave(rules: RulesConfig) -> None:
    prof = profile(employment_status=EmploymentStatus.NOTICE_PERIOD)
    res = check(rules, prof, leave_type_code="SL", start=date(2030, 3, 5), end=date(2030, 3, 5))
    assert res.result.verdict is Verdict.NOT_ELIGIBLE
    blocked = next(f for f in res.result.findings if f.code == "blocked_by_employment_status")
    assert blocked.policy_ref  # every rule carries its policy reference
    assert "serving the notice period" in blocked.message
    assert res.result.next_steps == []  # no "use another leave type" advice when status blocks


def test_unknown_type_weekend_only_and_year_span(rules: RulesConfig) -> None:
    unknown = check(rules, leave_type_code="XYZ", start=TODAY, end=TODAY).result
    assert unknown.verdict is Verdict.NOT_ELIGIBLE and "Casual Leave" in unknown.findings[0].message
    weekend = check(rules, leave_type_code="CL", start=date(2030, 3, 9), end=date(2030, 3, 10))
    assert "no_working_days" in {f.code for f in weekend.result.findings}
    span = check(rules, leave_type_code="EL", start=date(2030, 12, 30), end=date(2031, 1, 2))
    assert "spans_calendar_years" in {f.code for f in span.result.findings}


def test_overlapping_request_blocks(rules: RulesConfig) -> None:
    from app.db.models import LeaveRequestStatus
    from app.domain.hr import LeaveRequestView

    existing = LeaveRequestView(
        id=uuid.uuid4(),
        leave_type_code="EL",
        start_date=date(2030, 3, 5),
        end_date=date(2030, 3, 7),
        days=Decimal(2),
        status=LeaveRequestStatus.PENDING,
        reason=None,
    )
    res = check_leave_eligibility(
        profile(),
        LeaveRequestFacts("CL", date(2030, 3, 7), date(2030, 3, 8)),
        LEAVE_TYPES,
        BALANCES,
        HOLIDAYS,
        rules,
        TODAY,
        existing_requests=[existing],
    ).result
    assert "overlaps_existing_request" in {f.code for f in res.findings}
    cancelled = existing.model_copy(update={"status": LeaveRequestStatus.CANCELLED})
    ok = check_leave_eligibility(
        profile(),
        LeaveRequestFacts("CL", date(2030, 3, 7), date(2030, 3, 8)),
        LEAVE_TYPES,
        BALANCES,
        HOLIDAYS,
        rules,
        TODAY,
        existing_requests=[cancelled],
    ).result
    assert ok.verdict is Verdict.ELIGIBLE


def test_pending_requests_are_explained(rules: RulesConfig) -> None:
    res = check(rules, leave_type_code="EL", start=date(2030, 3, 5), end=date(2030, 3, 5)).result
    assert res.available_days == Decimal(5)
    assert "pending_requests" in {f.code for f in res.findings}


def test_past_dates_warn_but_do_not_block(rules: RulesConfig) -> None:
    res = check(rules, leave_type_code="SL", start=date(2030, 3, 1), end=date(2030, 3, 1)).result
    assert res.verdict is Verdict.ELIGIBLE
    assert "past_dates" in {f.code for f in res.findings}


def test_no_balances_on_record_routes_to_hr(rules: RulesConfig) -> None:
    res = check_leave_eligibility(
        profile(location=Location.USA),
        LeaveRequestFacts("CL", date(2030, 3, 5), date(2030, 3, 5)),
        LEAVE_TYPES,
        [],
        [],
        rules,
        TODAY,
    ).result
    assert res.verdict is Verdict.NEEDS_HR


def test_leave_policy_not_applicable_routes_to_hr_for_any_type(rules: RulesConfig) -> None:
    us = profile(location=Location.USA, leave_policy_applicable=False)
    for code in ("CL", "ML", "XYZ"):
        res = check(rules, us, leave_type_code=code, start=date(2030, 3, 5), end=date(2030, 3, 5))
        assert res.result.verdict is Verdict.NEEDS_HR
        assert [f.code for f in res.result.findings] == ["leave_policy_not_applicable"]
        assert res.working_days is None


def test_maternity_rules(rules: RulesConfig) -> None:
    m = rules.leave.maternity
    full_term = date(2030, 4, 1) + timedelta(days=m.weeks_first_two_children * 7 - 1)
    missing = check(rules, leave_type_code="ML", start=date(2030, 4, 1), end=date(2030, 6, 30))
    assert missing.result.verdict is Verdict.NEEDS_INFO
    assert missing.result.missing == ["expected_delivery_date"]

    ok = check(
        rules,
        leave_type_code="ML",
        start=date(2030, 4, 1),
        end=full_term,  # exactly the configured weeks for the first two children
        event_date=date(2030, 5, 1),
    ).result
    assert ok.verdict is Verdict.ELIGIBLE
    assert ok.available_days == Decimal(m.weeks_first_two_children * 7)

    too_early = check(
        rules,
        leave_type_code="ML",
        start=date(2030, 4, 1),
        end=date(2030, 5, 1),
        event_date=date(2030, 8, 1),
    ).result
    assert "starts_too_early" in {f.code for f in too_early.findings}

    probation = check(
        rules,
        profile(confirmation_date=None),
        leave_type_code="ML",
        start=date(2030, 4, 1),
        end=date(2030, 5, 1),
        event_date=date(2030, 5, 1),
    ).result
    assert "not_confirmed" in {f.code for f in probation.findings}

    third_child = check(
        rules,
        profile(children_on_record=2),
        leave_type_code="ML",
        start=date(2030, 4, 1),
        end=full_term,
        event_date=date(2030, 5, 1),
    ).result
    assert "exceeds_entitlement" in {f.code for f in third_child.findings}


def test_paternity_rules(rules: RulesConfig) -> None:
    days = rules.leave.paternity.days_per_event  # a working week or less: Mon 11 Mar onwards
    ok = check(
        rules,
        leave_type_code="PTL",
        start=date(2030, 3, 11),
        end=date(2030, 3, 10 + days),
        event_date=date(2030, 3, 20),
    ).result
    assert ok.verdict is Verdict.ELIGIBLE and ok.requested_days == Decimal(days)

    outside = check(
        rules,
        leave_type_code="PTL",
        start=date(2030, 3, 11),
        end=date(2030, 3, 12),
        event_date=date(2030, 5, 20),
    ).result
    assert "outside_window" in {f.code for f in outside.findings}

    limit = check(
        rules,
        profile(children_on_record=2),
        leave_type_code="PTL",
        start=date(2030, 3, 11),
        end=date(2030, 3, 12),
        event_date=date(2030, 3, 12),
    ).result
    assert "child_limit_reached" in {f.code for f in limit.findings}

    adoption = check(
        rules,
        leave_type_code="PTL",
        start=date(2030, 3, 11),
        end=date(2030, 3, 12),
        is_adoption=True,
    ).result
    assert adoption.missing == ["adoption_date"]


# ----------------------------------------------------------------------------- WFH


def test_experience_months() -> None:
    p = profile(date_of_joining=date(2028, 3, 10), prior_experience_months=5)
    assert total_experience_months(p, date(2030, 3, 9)) == 23 + 5
    assert total_experience_months(p, date(2030, 3, 10)) == 24 + 5


def test_wfh_eligibility(rules: RulesConfig) -> None:
    limit = rules.wfh.max_days_per_month
    ok = check_wfh_eligibility(
        profile(), days_used_this_month=limit - 2, requested_days=2, rules=rules, today=TODAY
    )
    assert ok.verdict is Verdict.ELIGIBLE and ok.available_days == Decimal(2)

    over = check_wfh_eligibility(
        profile(), days_used_this_month=limit, requested_days=1, rules=rules, today=TODAY
    )
    assert over.verdict is Verdict.ELIGIBLE
    assert "exceeds_monthly_limit" in {f.code for f in over.findings}

    junior = check_wfh_eligibility(
        profile(date_of_joining=date(2029, 6, 1), prior_experience_months=0),
        days_used_this_month=0,
        requested_days=1,
        rules=rules,
        today=TODAY,
    )
    assert junior.verdict is Verdict.NOT_ELIGIBLE

    notice = check_wfh_eligibility(
        profile(employment_status=EmploymentStatus.NOTICE_PERIOD), 0, 1, rules, TODAY
    )
    assert "blocked_by_employment_status" in {f.code for f in notice.findings}


# ----------------------------------------------------------------------------- location


def test_location_parsing_and_resolution() -> None:
    available = [Location.CHENNAI, Location.KARNATAKA, Location.USA]
    assert parse_location("Bengaluru") is Location.KARNATAKA
    assert parse_location("our Chennai office") is Location.CHENNAI

    explicit = resolve_location("bangalore", Location.CHENNAI, available)
    assert explicit.location is Location.KARNATAKA and not explicit.needs_confirmation

    implicit = resolve_location(None, Location.CHENNAI, available)
    assert implicit.location is Location.CHENNAI and implicit.from_profile
    assert not implicit.needs_confirmation and len(implicit.options) == 3

    unknown = resolve_location("Mumbai", Location.CHENNAI, available)
    assert unknown.needs_confirmation and unknown.message

    no_list = resolve_location(None, Location.USA, [Location.CHENNAI, Location.KARNATAKA])
    assert no_list.needs_confirmation and no_list.location is None and no_list.message


# ------------------------------------------------------------------- loans / certification


def _loan(disbursed: date, closed: date | None, outstanding: int = 0):  # type: ignore[no-untyped-def]
    from app.domain.hr import StaffLoanView

    return StaffLoanView(
        amount_inr=50000, disbursed_on=disbursed, closed_on=closed, outstanding_inr=outstanding
    )


def test_staff_loan_rules(rules: RulesConfig) -> None:
    from app.domain.rules.benefits import check_staff_loan_eligibility

    ok = check_staff_loan_eligibility(profile(annual_ctc_inr=900000), [], rules, TODAY)
    assert ok.verdict is Verdict.ELIGIBLE and ok.next_steps

    def codes(res):  # type: ignore[no-untyped-def]
        return {f.code for f in res.findings if f.blocking}

    junior = profile(date_of_joining=date(2030, 1, 15))  # under the minimum service
    assert "insufficient_service" in codes(check_staff_loan_eligibility(junior, [], rules, TODAY))
    assert "not_confirmed" in codes(
        check_staff_loan_eligibility(profile(confirmation_date=None), [], rules, TODAY)
    )
    assert "ctc_above_limit" in codes(
        check_staff_loan_eligibility(profile(annual_ctc_inr=2500000), [], rules, TODAY)
    )
    outstanding = [_loan(date(2029, 1, 10), None, 20000)]
    assert "previous_loan_outstanding" in codes(
        check_staff_loan_eligibility(profile(), outstanding, rules, TODAY)
    )
    recent = [_loan(date(2028, 1, 10), date(2029, 12, 31))]  # repaid < 6 months ago
    assert "gap_not_met" in codes(check_staff_loan_eligibility(profile(), recent, rules, TODAY))
    same_fy = [_loan(date(2029, 4, 15), date(2029, 7, 1))]  # FY Apr 2029 - Mar 2030
    assert "financial_year_limit" in codes(
        check_staff_loan_eligibility(profile(), same_fy, rules, TODAY)
    )
    assert "amount_above_limit" in codes(
        check_staff_loan_eligibility(profile(), [], rules, TODAY, requested_amount_inr=10**7)
    )


def test_certification_rules(rules: RulesConfig) -> None:
    from app.domain.rules.benefits import check_certification_reimbursement

    needs = check_certification_reimbursement(profile(), rules, TODAY)
    assert needs.verdict is Verdict.NEEDS_INFO and needs.missing == ["completion_date"]

    within = check_certification_reimbursement(profile(), rules, TODAY, date(2030, 2, 20))
    assert within.verdict is Verdict.ELIGIBLE
    assert "claim_deadline" in {f.code for f in within.findings}

    late = check_certification_reimbursement(profile(), rules, TODAY, date(2029, 12, 1))
    assert late.verdict is Verdict.NOT_ELIGIBLE

    junior = profile(date_of_joining=date(2030, 1, 15))  # under the minimum service
    assert (
        check_certification_reimbursement(junior, rules, TODAY, date(2030, 3, 1)).verdict
        is Verdict.NOT_ELIGIBLE
    )


def test_inr_formatting() -> None:
    from app.domain.rules.benefits import inr

    assert (inr(2000000), inr(37500), inr(999), inr(100000)) == (
        "₹20,00,000",
        "₹37,500",
        "₹999",
        "₹1,00,000",
    )
