"""Typed eligibility-rule configuration, loaded from YAML.

Rule *values* and policy references come from configuration (the private file transcribed from
the current policies), so HR can change a number without a code change and the public repository
holds no policy content.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from app.db.models import EmploymentStatus

PRIVATE_RULES = Path("data/private_policies/config/rules.yaml")
EXAMPLE_RULES = Path("data/seed/rules.example.yaml")

Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CalendarRules(_Strict):
    weekend_days: list[Weekday]
    ref: str


class MaternityRules(_Strict):
    code: str
    min_confirmed_service_days: int
    weeks_first_two_children: int
    weeks_third_child_onwards: int
    adoption_weeks: int
    adoption_max_child_age_months: int
    max_weeks_before_delivery: int
    document: str
    ref: str


class PaternityRules(_Strict):
    code: str
    days_per_event: int
    max_children: int
    window_weeks: int
    document: str
    ref: str


class LeaveRules(_Strict):
    approval: str
    approval_ref: str
    blocked_statuses: list[EmploymentStatus]
    blocked_types: list[str]
    blocked_ref: str
    unapproved_absence: str
    unapproved_ref: str
    maternity: MaternityRules
    paternity: PaternityRules


class WfhRules(_Strict):
    max_days_per_month: int
    max_days_per_week: int
    min_experience_months: int
    experience_exception: str
    blocked_statuses: list[EmploymentStatus]
    excess_handling: str
    exception_approval: str
    ref: str
    blocked_ref: str


class StaffLoanRules(_Strict):
    min_service_months: int
    gap_months_after_repayment: int
    max_loans_per_financial_year: int
    financial_year_start_month: int
    max_annual_ctc_inr: int
    max_amount_inr: int
    max_emi_months: int
    purposes: list[str]
    process: str
    ref: str


class CertificationRules(_Strict):
    min_service_months: int
    claim_window_days: int
    excluded_examples: list[str]
    service_commitment_months: int
    process: str
    ref: str


class RulesConfig(_Strict):
    calendar: CalendarRules
    leave: LeaveRules
    wfh: WfhRules
    staff_loan: StaffLoanRules
    certification: CertificationRules


def load_rules(path: Path | None = None) -> RulesConfig:
    chosen = path or (PRIVATE_RULES if PRIVATE_RULES.exists() else EXAMPLE_RULES)
    return RulesConfig.model_validate(yaml.safe_load(chosen.read_text(encoding="utf-8")))
