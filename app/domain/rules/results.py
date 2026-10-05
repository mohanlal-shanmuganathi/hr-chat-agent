"""Structured verdicts returned by the rules engine.

Every verdict lists the rules it applied, each with a policy reference, so the agent can explain
*why* and cite the source instead of improvising.
"""

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Verdict(StrEnum):
    ELIGIBLE = "eligible"
    NOT_ELIGIBLE = "not_eligible"
    NEEDS_INFO = "needs_info"  # the user must supply something (see `missing`) before deciding
    NEEDS_HR = "needs_hr"  # rules cannot decide (policy gap or discretionary); route to HR


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str  # machine-readable, e.g. "insufficient_balance"
    message: str  # plain-language explanation
    policy_ref: str | None = None
    blocking: bool = False


class EligibilityResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    verdict: Verdict
    requested_days: Decimal | None = None
    available_days: Decimal | None = None
    findings: list[Finding]
    missing: list[str] = []
    next_steps: list[str] = []

    @classmethod
    def from_findings(
        cls,
        findings: list[Finding],
        *,
        needs_hr: bool = False,
        missing: list[str] | None = None,
        requested_days: Decimal | None = None,
        available_days: Decimal | None = None,
        next_steps: list[str] | None = None,
    ) -> "EligibilityResult":
        if any(f.blocking for f in findings):
            verdict = Verdict.NOT_ELIGIBLE
        elif missing:
            verdict = Verdict.NEEDS_INFO
        elif needs_hr:
            verdict = Verdict.NEEDS_HR
        else:
            verdict = Verdict.ELIGIBLE
        return cls(
            verdict=verdict,
            requested_days=requested_days,
            available_days=available_days,
            findings=findings,
            missing=missing or [],
            next_steps=next_steps or [],
        )
