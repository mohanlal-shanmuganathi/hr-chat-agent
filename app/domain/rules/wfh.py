"""Work-from-home eligibility and monthly allowance."""

from datetime import date
from decimal import Decimal

from app.domain.hr import EmployeeProfile
from app.domain.rules.config import RulesConfig
from app.domain.rules.results import EligibilityResult, Finding


def total_experience_months(profile: EmployeeProfile, today: date) -> int:
    tenure = (today.year - profile.date_of_joining.year) * 12 + (
        today.month - profile.date_of_joining.month
    )
    if today.day < profile.date_of_joining.day:
        tenure -= 1
    return max(tenure, 0) + profile.prior_experience_months


def check_wfh_eligibility(
    profile: EmployeeProfile,
    days_used_this_month: int,
    requested_days: int,
    rules: RulesConfig,
    today: date,
) -> EligibilityResult:
    w = rules.wfh
    findings: list[Finding] = []

    if profile.employment_status in w.blocked_statuses:
        findings.append(
            Finding(
                code="blocked_by_employment_status",
                message="Work from home is not available while serving the notice period.",
                policy_ref=w.blocked_ref,
                blocking=True,
            )
        )

    months = total_experience_months(profile, today)
    if months < w.min_experience_months:
        findings.append(
            Finding(
                code="insufficient_experience",
                message=f"WFH needs at least {w.min_experience_months // 12} years of experience; "
                f"you have {months // 12} year(s) {months % 12} month(s). "
                f"{w.experience_exception}",
                policy_ref=w.ref,
                blocking=True,
            )
        )

    remaining = max(w.max_days_per_month - days_used_this_month, 0)
    findings.append(
        Finding(
            code="monthly_allowance",
            message=f"You have used {days_used_this_month} of {w.max_days_per_month} WFH days "
            f"this month ({remaining} left; at most {w.max_days_per_week} per week, scheduled by "
            "your project manager).",
            policy_ref=w.ref,
        )
    )
    if requested_days > remaining:
        findings.append(
            Finding(
                code="exceeds_monthly_limit",
                message=f"{requested_days - remaining} requested day(s) exceed the monthly limit. "
                f"{w.excess_handling} {w.exception_approval}",
                policy_ref=w.ref,
            )
        )
    findings.append(
        Finding(
            code="manager_discretion",
            message="WFH may also be unavailable during client escalations, high-risk projects, "
            "critical deliveries or training; your manager decides those cases.",
            policy_ref=w.ref,
        )
    )
    return EligibilityResult.from_findings(
        findings,
        requested_days=Decimal(requested_days),
        available_days=Decimal(remaining),
    )
