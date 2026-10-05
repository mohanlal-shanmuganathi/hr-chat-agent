"""Staff-loan and certification-reimbursement eligibility.

Only the objective criteria are decided here (service, loan history, CTC limit, claim window).
Discretionary parts (management approval, whether a certification is role-relevant) are reported
as such and never decided by the engine.
"""

from datetime import date, timedelta

from app.db.models import EmploymentStatus
from app.domain.hr import EmployeeProfile, StaffLoanView
from app.domain.rules.config import RulesConfig
from app.domain.rules.results import EligibilityResult, Finding


def inr(amount: int) -> str:
    """Indian digit grouping: 1234567 -> ₹12,34,567."""
    digits = str(abs(amount))
    head, tail = digits[:-3], digits[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return "₹" + ",".join([*groups, tail])


def months_between(start: date, end: date) -> int:
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return max(months, 0)


def add_months(day: date, months: int) -> date:
    year, month = divmod(day.month - 1 + months, 12)
    year += day.year
    month += 1
    last_day = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(day.day, last_day))


def financial_year_start(day: date, start_month: int) -> date:
    year = day.year if day.month >= start_month else day.year - 1
    return date(year, start_month, 1)


def check_staff_loan_eligibility(
    profile: EmployeeProfile,
    loans: list[StaffLoanView],
    rules: RulesConfig,
    today: date,
    requested_amount_inr: int | None = None,
) -> EligibilityResult:
    r = rules.staff_loan
    findings: list[Finding] = []

    if profile.employment_status is not EmploymentStatus.ACTIVE:
        findings.append(
            Finding(
                code="not_active",
                message="Staff loans are for active employees; any outstanding loan becomes due "
                "on exit.",
                policy_ref=r.ref,
                blocking=True,
            )
        )
    if profile.confirmation_date is None:
        findings.append(
            Finding(
                code="not_confirmed",
                message="Your employment must be confirmed; you are still on probation.",
                policy_ref=r.ref,
                blocking=True,
            )
        )
    service = months_between(profile.date_of_joining, today)
    if service < r.min_service_months:
        eligible_from = add_months(profile.date_of_joining, r.min_service_months)
        findings.append(
            Finding(
                code="insufficient_service",
                message=f"You need {r.min_service_months} months of service; you have {service}. "
                f"You meet this on {eligible_from:%d %b %Y}.",
                policy_ref=r.ref,
                blocking=True,
            )
        )
    if profile.annual_ctc_inr is not None and profile.annual_ctc_inr > r.max_annual_ctc_inr:
        findings.append(
            Finding(
                code="ctc_above_limit",
                message=f"Employees with CTC above {inr(r.max_annual_ctc_inr)} are not eligible.",
                policy_ref=r.ref,
                blocking=True,
            )
        )

    open_loans = [ln for ln in loans if ln.closed_on is None]
    if open_loans:
        findings.append(
            Finding(
                code="previous_loan_outstanding",
                message=f"Your previous loan has {inr(open_loans[0].outstanding_inr)} outstanding; "
                "it must be fully repaid first.",
                policy_ref=r.ref,
                blocking=True,
            )
        )
    closed = sorted((ln.closed_on for ln in loans if ln.closed_on), reverse=True)
    if closed:
        allowed_from = add_months(closed[0], r.gap_months_after_repayment)
        if today < allowed_from:
            findings.append(
                Finding(
                    code="gap_not_met",
                    message=f"A {r.gap_months_after_repayment}-month gap is needed after repaying "
                    f"the previous loan (repaid {closed[0]:%d %b %Y}); you can apply from "
                    f"{allowed_from:%d %b %Y}.",
                    policy_ref=r.ref,
                    blocking=True,
                )
            )
    fy_start = financial_year_start(today, r.financial_year_start_month)
    this_fy = [ln for ln in loans if ln.disbursed_on >= fy_start]
    if len(this_fy) >= r.max_loans_per_financial_year:
        findings.append(
            Finding(
                code="financial_year_limit",
                message=f"Only {r.max_loans_per_financial_year} loan per financial year "
                f"(from {fy_start:%d %b %Y}); you already have one.",
                policy_ref=r.ref,
                blocking=True,
            )
        )
    if requested_amount_inr is not None and requested_amount_inr > r.max_amount_inr:
        findings.append(
            Finding(
                code="amount_above_limit",
                message=f"The maximum loan is {inr(r.max_amount_inr)}.",
                policy_ref=r.ref,
                blocking=True,
            )
        )
    findings.append(
        Finding(
            code="terms",
            message=f"Up to {inr(r.max_amount_inr)}, repaid over at most {r.max_emi_months} "
            "monthly instalments from salary."
            f" Allowed purposes: {', '.join(r.purposes)}. Approval and "
            "final amount are at management's discretion.",
            policy_ref=r.ref,
        )
    )
    return EligibilityResult.from_findings(
        findings,
        next_steps=[r.process] if not any(f.blocking for f in findings) else [],
    )


def check_certification_reimbursement(
    profile: EmployeeProfile,
    rules: RulesConfig,
    today: date,
    completion_date: date | None = None,
) -> EligibilityResult:
    c = rules.certification
    findings: list[Finding] = []
    missing: list[str] = []

    service = months_between(profile.date_of_joining, today)
    if service < c.min_service_months:
        findings.append(
            Finding(
                code="insufficient_service",
                message=f"You need {c.min_service_months} months of service; you have {service}.",
                policy_ref=c.ref,
                blocking=True,
            )
        )
    if profile.employment_status is not EmploymentStatus.ACTIVE:
        findings.append(
            Finding(
                code="not_active",
                message=f"Reimbursement requires serving {c.service_commitment_months} more "
                "months after it is paid, which is not possible while exiting.",
                policy_ref=c.ref,
                blocking=True,
            )
        )
    if completion_date is None:
        missing.append("completion_date")
    elif completion_date > today:
        findings.append(
            Finding(
                code="not_completed_yet",
                message="Fees are reimbursed only after you pass. Get manager approval before "
                "you start.",
                policy_ref=c.ref,
            )
        )
    else:
        deadline = completion_date + timedelta(days=c.claim_window_days)
        if today > deadline:
            findings.append(
                Finding(
                    code="claim_window_passed",
                    message=f"Claims must be made within {c.claim_window_days} days of "
                    f"completion; the deadline was {deadline:%d %b %Y}.",
                    policy_ref=c.ref,
                    blocking=True,
                )
            )
        else:
            findings.append(
                Finding(
                    code="claim_deadline",
                    message=f"Claim by {deadline:%d %b %Y} ({(deadline - today).days} days left).",
                    policy_ref=c.ref,
                )
            )
    findings.append(
        Finding(
            code="discretionary_checks",
            message="The certification must be business- and role-relevant and approved in "
            "advance by your manager; fundamentals-level certifications (e.g. "
            f"{', '.join(c.excluded_examples)}) are not reimbursed. You commit to "
            f"{c.service_commitment_months} months of service after reimbursement.",
            policy_ref=c.ref,
        )
    )
    return EligibilityResult.from_findings(
        findings,
        missing=missing,
        next_steps=[c.process] if not any(f.blocking for f in findings) else [],
    )
