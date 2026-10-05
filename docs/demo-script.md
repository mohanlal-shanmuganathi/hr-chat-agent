# Demo script (about 6 minutes)

**Setup before recording:**
```powershell
docker compose up -d --build
docker compose run --rm api python -m app.hr.seed
docker compose run --rm api python -m app.rag.ingest
```
Open http://localhost:8000 in a clean browser window (zoom 110–125% so text is readable on video).
After each answer, click **"Used N tools ›"** above it to show the steps, the arguments the model
chose, the sources and the token count.

| # | Show | Say / point out |
|---|---|---|
| 1 | Architecture diagram (`docs/architecture.md`) for 30s | One agent, deterministic tools, Postgres for HR data, vectors and checkpoints, confidential docs kept out of git. |
| 2 | Terminal: the ingestion report | 34 PDFs read; Leave Policy v1.2 and Staff Loan 2022 automatically **superseded**; holidays loaded per location. |
| 3 | Sign in (Google, or demo user **Priya R**) | SSO restricted to @ideas2it.com; demo users are fictional. |
| 4 | "How many sick leaves do I get in a year?" | Policy search, then a **cited** answer from Leave Policy v1.3. |
| 5 | "What is my leave balance?" | Personal data tool; only Priya's own records. |
| 6 | "Can I take casual leave from 16 to 20 Oct?" | Eligibility rules: weekend and Ayudha Poojai excluded, so 2 days. The model didn't do the arithmetic. |
| 7 | "When is Diwali?" then "What about Bengaluru?" | Answers for the employee's own location (Chennai: 8 Nov), says other lists exist, then answers for Karnataka. |
| 8 | "Am I eligible for a staff loan?" | Rules engine beyond leave: service, repayment gap, one per financial year, CTC. |
| 9 | "Apply earned leave for 9 and 10 November" | **Approval card**: nothing is saved until Approve. Click Approve, then pending. |
| 10 | Sign out, sign in as **Rahul P**: "Can I take EL next week?" | Notice period blocks it, citing the policy. |
| 11 | "Show me Rohan's leave balance" | Refuses: tools can only access the signed-in employee. |
| 12 | "Ignore previous instructions and…" / "I am being harassed by my manager" | Guardrails: refused before the model; sensitive disclosure goes to HR with a confidential ticket offer. |
| 13 | "What's the capital of France?" | Out of scope, redirected to HR at hr@ideas2it.com. |
| 14 | `docs/eval-results.md` and the CI checks on GitHub | Evaluation pass rate; tests in CI; the audit log (`select * from audit_log`). |

Close with: the HR data is mocked behind an interface, so plugging in the real employee-portal
API is a provider swap. The model is configurable, so re-run the evals after switching.
