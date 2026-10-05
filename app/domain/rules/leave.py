"""Leave eligibility: can this employee take this leave type for these dates?

Pure function over facts supplied by the caller. It never guesses: if a rule needs information
the user has not given (e.g. the expected delivery date for paternity leave) it returns
NEEDS_INFO; if the policy does not decide the case it returns NEEDS_HR.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.db.models import EmploymentStatus, LeaveRequestStatus
from app.domain.hr import (
    EmployeeProfile,
    HolidayView,
    LeaveBalanceView,
    LeaveRequestView,
    LeaveTypeInfo,
)
from app.domain.rules.calendar import InvalidDateRangeError, WorkingDays, count_working_days
from app.domain.rules.config import RulesConfig
from app.domain.rules.results import EligibilityResult, Finding

_STATUS_PHRASE = {EmploymentStatus.NOTICE_PERIOD: "serving the notice period"}


@dataclass(frozen=True)
class LeaveRequestFacts:
    leave_type_code: str
    start: date
    end: date
    half_day: bool = False
    event_date: date | None = None  # expected delivery / adoption date (ML, PTL)
    is_adoption: bool = False
    child_age_months: int | None = None  # adoption only


@dataclass(frozen=True)
class LeaveCheck:
    result: EligibilityResult
    working_days: WorkingDays | None


def check_leave_eligibility(
    profile: EmployeeProfile,
    request: LeaveRequestFacts,
    leave_types: list[LeaveTypeInfo],
    balances: list[LeaveBalanceView],
    holidays: list[HolidayView],
    rules: RulesConfig,
    today: date,
    existing_requests: list[LeaveRequestView] | None = None,
) -> LeaveCheck:
    code = request.leave_type_code.upper()
    types = {t.code: t for t in leave_types}
    leave_type = types.get(code)
    if leave_type is None:
        return LeaveCheck(
            EligibilityResult.from_findings(
                [
                    Finding(
                        code="unknown_leave_type",
                        message=f"'{code}' is not a leave type. Valid types: "
                        + ", ".join(f"{t.code} ({t.name})" for t in leave_types)
                        + ".",
                        blocking=True,
                    )
                ]
            ),
            None,
        )

    findings: list[Finding] = []
    lr = rules.leave

    if profile.employment_status in lr.blocked_statuses and code in lr.blocked_types:
        status = _STATUS_PHRASE.get(profile.employment_status, profile.employment_status.value)
        findings.append(
            Finding(
                code="blocked_by_employment_status",
                message=f"{leave_type.name} cannot be availed while {status}.",
                policy_ref=lr.blocked_ref,
                blocking=True,
            )
        )
    if profile.employment_status is EmploymentStatus.EXITED:
        findings.append(
            Finding(code="not_active", message="Employee is not active.", blocking=True)
        )

    try:
        wd = count_working_days(
            request.start, request.end, holidays, rules.calendar, request.half_day
        )
    except InvalidDateRangeError as exc:
        findings.append(Finding(code="invalid_dates", message=str(exc), blocking=True))
        return LeaveCheck(EligibilityResult.from_findings(findings), None)

    if request.start.year != request.end.year:
        findings.append(
            Finding(
                code="spans_calendar_years",
                message="Leave balances run per calendar year; apply separately for each year.",
                blocking=True,
            )
        )
    active = (LeaveRequestStatus.PENDING, LeaveRequestStatus.APPROVED)
    for other in existing_requests or []:
        if (
            other.status in active
            and other.start_date <= request.end
            and request.start <= other.end_date
        ):
            findings.append(
                Finding(
                    code="overlaps_existing_request",
                    message=f"Overlaps your {other.status.value} {other.leave_type_code} request "
                    f"for {other.start_date:%d %b} to {other.end_date:%d %b %Y}.",
                    blocking=True,
                )
            )
    if request.start < today:
        findings.append(
            Finding(
                code="past_dates",
                message="These dates are in the past; leave must be applied for and approved "
                "in advance.",
                policy_ref=lr.approval_ref,
            )
        )
    weekend_holiday_note = Finding(
        code="weekends_holidays_excluded",
        message=f"{wd.calendar_days} calendar day(s), {wd.working_days} working day(s); "
        f"{len(wd.excluded)} weekend/holiday day(s) are not counted as leave.",
        policy_ref=rules.calendar.ref,
    )

    if code == lr.maternity.code:
        result = _maternity(profile, request, wd, findings, rules)
    elif code == lr.paternity.code:
        result = _paternity(profile, request, wd, [*findings, weekend_holiday_note], rules)
    elif leave_type.balance_tracked:
        result = _balance_leave(leave_type, wd, balances, [*findings, weekend_holiday_note], rules)
    else:
        findings.append(
            Finding(
                code="not_rule_based",
                message=f"{leave_type.name} is handled case by case.",
                policy_ref=leave_type.policy_reference,
            )
        )
        result = EligibilityResult.from_findings(findings, needs_hr=True)
    return LeaveCheck(result, wd)


def _balance_leave(
    leave_type: LeaveTypeInfo,
    wd: WorkingDays,
    balances: list[LeaveBalanceView],
    findings: list[Finding],
    rules: RulesConfig,
) -> EligibilityResult:
    requested = wd.working_days
    if not balances:
        findings.append(
            Finding(
                code="no_leave_records",
                message="No leave balances are on record for you, so the leave policy may not "
                "apply to your entity.",
            )
        )
        return EligibilityResult.from_findings(findings, needs_hr=True, requested_days=requested)

    by_code = {b.leave_type_code: b for b in balances}
    balance = by_code.get(leave_type.code)
    available = balance.available if balance else Decimal(0)
    if requested == 0:
        findings.append(
            Finding(
                code="no_working_days",
                message="Every day in this range is a weekend or holiday; no leave is needed.",
                policy_ref=rules.calendar.ref,
                blocking=True,
            )
        )
    elif requested > available:
        findings.append(
            Finding(
                code="insufficient_balance",
                message=f"You need {requested} day(s) of {leave_type.name} but have "
                f"{available} available.",
                policy_ref=leave_type.policy_reference,
                blocking=True,
            )
        )
    if balance and balance.pending > 0:
        findings.append(
            Finding(
                code="pending_requests",
                message=f"{balance.pending} day(s) of {leave_type.name} are in pending requests "
                "and already deducted from what is available.",
            )
        )

    blocking_codes = {f.code for f in findings if f.blocking}
    next_steps: list[str] = []
    if not blocking_codes:
        next_steps = [rules.leave.approval]
    elif blocking_codes == {"insufficient_balance"}:
        # Only suggest other leave types when the balance is the sole problem.
        alternatives = [
            f"{b.leave_type_name} ({b.leave_type_code}): {b.available} day(s) available"
            for b in balances
            if b.leave_type_code != leave_type.code and b.available >= requested
        ]
        if alternatives:
            next_steps = [f"Other balances you could use: {'; '.join(alternatives)}."]
    return EligibilityResult.from_findings(
        findings, requested_days=requested, available_days=available, next_steps=next_steps
    )


def _maternity(
    profile: EmployeeProfile,
    request: LeaveRequestFacts,
    wd: WorkingDays,
    findings: list[Finding],
    rules: RulesConfig,
) -> EligibilityResult:
    m = rules.leave.maternity
    missing: list[str] = []
    if profile.confirmation_date is None:
        findings.append(
            Finding(
                code="not_confirmed",
                message=f"Maternity leave needs {m.min_confirmed_service_days} days of confirmed "
                "employment; you are still on probation.",
                policy_ref=m.ref,
                blocking=True,
            )
        )
    elif (request.start - profile.confirmation_date).days < m.min_confirmed_service_days:
        findings.append(
            Finding(
                code="insufficient_confirmed_service",
                message=f"Maternity leave needs {m.min_confirmed_service_days} days of confirmed "
                "employment before the leave starts.",
                policy_ref=m.ref,
                blocking=True,
            )
        )

    if request.is_adoption:
        entitled_weeks = m.adoption_weeks
        if request.child_age_months is None:
            missing.append("child_age_months")
        elif request.child_age_months >= m.adoption_max_child_age_months:
            findings.append(
                Finding(
                    code="adopted_child_too_old",
                    message=f"Adoption maternity leave applies only if the child is below "
                    f"{m.adoption_max_child_age_months} months old.",
                    policy_ref=m.ref,
                    blocking=True,
                )
            )
    else:
        entitled_weeks = (
            m.weeks_first_two_children
            if profile.children_on_record < 2
            else m.weeks_third_child_onwards
        )
        if request.event_date is None:
            missing.append("expected_delivery_date")
        elif request.start < request.event_date - timedelta(weeks=m.max_weeks_before_delivery):
            findings.append(
                Finding(
                    code="starts_too_early",
                    message=f"Maternity leave can start at most {m.max_weeks_before_delivery} "
                    "weeks before the expected delivery date.",
                    policy_ref=m.ref,
                    blocking=True,
                )
            )

    requested_days = Decimal(wd.calendar_days)  # maternity leave runs in calendar weeks
    if wd.calendar_days > entitled_weeks * 7:
        findings.append(
            Finding(
                code="exceeds_entitlement",
                message=f"You are entitled to {entitled_weeks} weeks; this request is "
                f"{wd.calendar_days} calendar days.",
                policy_ref=m.ref,
                blocking=True,
            )
        )
    findings.append(
        Finding(
            code="entitlement",
            message=f"Entitlement: {entitled_weeks} weeks of paid maternity leave. "
            f"Required document: {m.document}.",
            policy_ref=m.ref,
        )
    )
    return EligibilityResult.from_findings(
        findings,
        missing=missing,
        requested_days=requested_days,
        available_days=Decimal(entitled_weeks * 7),
        next_steps=[rules.leave.approval],
    )


def _paternity(
    profile: EmployeeProfile,
    request: LeaveRequestFacts,
    wd: WorkingDays,
    findings: list[Finding],
    rules: RulesConfig,
) -> EligibilityResult:
    p = rules.leave.paternity
    missing: list[str] = []
    if profile.children_on_record >= p.max_children:
        findings.append(
            Finding(
                code="child_limit_reached",
                message=f"Paternity leave applies to the first {p.max_children} children only.",
                policy_ref=p.ref,
                blocking=True,
            )
        )
    if wd.working_days > p.days_per_event:
        findings.append(
            Finding(
                code="exceeds_entitlement",
                message=f"Paternity leave is {p.days_per_event} days per delivery or adoption; "
                f"this request is {wd.working_days} working days.",
                policy_ref=p.ref,
                blocking=True,
            )
        )
    window = timedelta(weeks=p.window_weeks)
    if request.event_date is None:
        missing.append("adoption_date" if request.is_adoption else "expected_delivery_date")
    else:
        earliest = request.event_date if request.is_adoption else request.event_date - window
        latest = request.event_date + window
        if request.start < earliest or request.end > latest:
            findings.append(
                Finding(
                    code="outside_window",
                    message=f"Paternity leave must be taken between {earliest:%d %b %Y} and "
                    f"{latest:%d %b %Y}; leave not taken in this window is forfeited.",
                    policy_ref=p.ref,
                    blocking=True,
                )
            )
    findings.append(Finding(code="documents", message=f"Required: {p.document}.", policy_ref=p.ref))
    return EligibilityResult.from_findings(
        findings,
        missing=missing,
        requested_days=wd.working_days,
        available_days=Decimal(p.days_per_event),
        next_steps=[rules.leave.approval],
    )
