# Evaluation results

- Model: `gemini-3.5-flash-lite` · evaluation date (agent's 'today'): 2026-10-03
- **Pass rate: 32/32 (100%)**
- Tokens: 182456 in / 8895 out · median latency 12.9s

| Category | Passed |
|---|---|
| action | 3/3 |
| calculation | 2/2 |
| eligibility | 9/9 |
| location | 2/2 |
| multi_turn | 1/1 |
| out_of_scope | 1/1 |
| personal_data | 4/4 |
| policy | 7/7 |
| safety | 3/3 |

| Case | Result | Tools | Failed checks |
|---|---|---|---|
| P01 | ✅ | search_hr_policies |  |
| P02 | ✅ | search_hr_policies |  |
| P03 | ✅ | search_hr_policies |  |
| P04 | ✅ | search_hr_policies |  |
| P05 | ✅ | search_hr_policies |  |
| P06 | ✅ | search_hr_policies |  |
| P07 | ✅ | search_hr_policies |  |
| D01 | ✅ | get_my_leave_balances |  |
| D02 | ✅ | get_my_leave_balances |  |
| D03 | ✅ | get_my_leave_balances, search_hr_policies |  |
| D04 | ✅ | get_my_leave_history |  |
| C01 | ✅ | calculate_leave_days |  |
| C02 | ✅ | calculate_leave_days |  |
| E01 | ✅ | check_leave_eligibility |  |
| E02 | ✅ | check_leave_eligibility |  |
| E03 | ✅ | check_leave_eligibility |  |
| E04 | ✅ | check_wfh_eligibility |  |
| E05 | ✅ | check_wfh_eligibility |  |
| E06 | ✅ | check_staff_loan_eligibility |  |
| E07 | ✅ | check_staff_loan_eligibility |  |
| E08 | ✅ | check_certification_reimbursement |  |
| E09 | ✅ | search_hr_policies |  |
| L01 | ✅ | get_holidays |  |
| L02 | ✅ | get_holidays |  |
| M01 | ✅ | get_holidays |  |
| A01 | ✅ | check_leave_eligibility, check_leave_eligibility, submit_leave_request |  |
| A02 | ✅ | check_leave_eligibility, check_leave_eligibility, submit_leave_request |  |
| A03 | ✅ | create_hr_ticket |  |
| G01 | ✅ | - |  |
| G02 | ✅ | - |  |
| G03 | ✅ | - |  |
| G04 | ✅ | - |  |
