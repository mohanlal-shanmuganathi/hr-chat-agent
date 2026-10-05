# Tools

All tools live in `app/tools/hr_tools.py` and run through one executor (`app/tools/base.py`):

- **Authentication:** the employee comes from the session as `ToolContext`. No tool takes an
  employee argument, and unknown arguments are rejected.
- **Validation:** Pydantic input models (`extra="forbid"`). Date ranges must be ordered, at most
  one year, and a half day must be a single day.
- **Timeout:** 8s by default (`TOOL_TIMEOUT_S`).
- **Errors:** `{"ok": false, "error": {"code", "message", "retryable"}}`. Codes include
  `invalid_arguments`, `timeout`, `not_allowed`, `unknown_location`, `invalid_dates`,
  `declined_by_user` and `internal_error`. Internal exception text is never returned.
- **Audit:** every call is written to `audit_log` with arguments, outcome and latency. Free-text
  fields (`reason`, `summary`) are stored only as `<redacted N chars>`.

| Tool | Inputs | Output | Side effects | Idempotent | Notes |
|---|---|---|---|---|---|
| `search_hr_policies` | `query`, `location?` | Up to 5 passages: citation, document, version, effective date, `applies_to`, `applies_to_you`, text (≤2,500 chars); an `applicability_note` when a passage does not cover the employee | none | yes | Current versions only; location-filtered; passages labelled as reference data; policies scoped to other locations stay searchable but are flagged |
| `get_my_profile` | – | Location, department, designation, joining date, probation, status, manager, experience, `leave_policy_applies` | none | yes | CTC is never returned |
| `get_my_leave_balances` | `year?` | Per type: carried forward, credited, used, pending, available | none | yes | `leave_policy_applies: false` with an HR note when the leave policy does not cover the employee (e.g. US entity); empty with HR contact if no records |
| `get_my_leave_history` | `year?` | Requests with dates, days, status | none | yes | |
| `get_holidays` | `location?`, `year?`, `month?` | Holidays for the named location, or the employee's location on record, with the other available locations; `needs_location_confirmation` only for an unknown location | none | yes | Same result whatever the model does: the rule is in code |
| `calculate_leave_days` | `start`, `end`, `half_day?` | Calendar days, leave days needed, each excluded day with its reason | none | yes | Uses the employee's location calendar; `leave_policy_applies: false` instead when the leave policy does not cover them |
| `check_leave_eligibility` | `leave_type`, `start`, `end`, `half_day?`, `expected_event_date?`, `is_adoption?`, `child_age_months?` | Verdict (`eligible`, `not_eligible`, `needs_info`, `needs_hr`), findings with policy references, missing fields, next steps | none | yes | Balance net of pending; notice period; overlaps; year span; maternity and paternity rules; `needs_hr` (`leave_policy_not_applicable`) when the leave policy does not cover the employee |
| `check_wfh_eligibility` | `requested_days?`, `month?` | Verdict, remaining days this month, findings | none | yes | Experience threshold, monthly allowance, excess handling |
| `check_staff_loan_eligibility` | `requested_amount_inr?` | Verdict, findings, process | none | yes | Confirmation and service, outstanding loan, repayment gap, one per financial year, CTC and amount limits |
| `check_certification_reimbursement` | `completion_date?`, `certification_name?` | Verdict, claim deadline, findings | none | yes | Role relevance and fundamentals exclusion are reported as the manager's call |
| `submit_leave_request` ✋ | as eligibility, plus `reason?` | Request id, status `pending`, days, `already_existed` | creates a pending leave request | **yes** (key = employee + type + dates + half day) | Needs the employee's approval; rules re-checked server-side; the key is checked first so retries return the original |
| `create_hr_ticket` ✋ | `category` (leave, payroll, policy_clarification, grievance, harassment, other), `summary` (10–1000 chars) | Ticket id, status, HR contact, `already_existed` | creates an HR ticket | **yes** (key = employee + category + summary) | Needs the employee's approval |

✋ = `requires_confirmation`: the graph pauses with `interrupt()` and resumes only after
Approve/Reject.

Rule values (entitlements, limits, windows, references) are configuration, not code:
`data/private_policies/config/rules.yaml` and `leave_types.yaml` (private), with placeholders in
`data/seed/*.example.yaml`. Which locations each policy covers is configured the same way in
`policy_scope.yaml`; it is applied at ingestion and shown as `applies_to` on search results.
