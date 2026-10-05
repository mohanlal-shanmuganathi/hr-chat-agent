# Agent specification: HR Assistant

| Field | Specification |
|---|---|
| **Agent** | HR Assistant (single LangGraph agent, `app/agent/graph.py`) |
| **Purpose** | Answer employees' HR and company-policy questions from current policy documents and their own HR data; decide eligibility through deterministic rules; submit leave requests and open HR tickets when asked, with confirmation. |
| **Inputs** | The employee's message. The server adds identity (employee id, name, location) and the current date from the authenticated session, plus the last 24 messages of the thread. |
| **Outputs** | Answer text with `[citations]`. Turn metadata: tools used (with arguments and outcome), citations, token usage, cost estimate, guardrail outcome, prompt version. Or a pending action that awaits approval. |
| **Tools** | 12 tools: policy search; own profile, balances and history; holidays; working-day calculation; leave, WFH, staff-loan and certification eligibility; `submit_leave_request`*; `create_hr_ticket`* (* = write, needs confirmation). See [tools.md](tools.md). |
| **Permissions** | Read: the caller's own HR records and the current policy corpus. Write: a pending leave request and an HR ticket for the caller, only after explicit approval. Nothing else. |
| **State** | LangGraph state (`messages`, `tool_rounds`, `guard`) checkpointed in Postgres per `employee_id:thread_id`. |
| **Memory** | Short-term: the thread history, trimmed to the last 24 messages. **No long-term memory, deliberately:** HR facts must come from the systems of record, and stored "memories" would go stale and create privacy risk. Knowledge comes from retrieval. |
| **System instructions** | `app/agent/prompts/system_v4.md` (versioned, earlier versions kept; the version is logged with every turn). |
| **Scope** | HR, the workplace and company policies only. A `scope_check` node (one structured model call on the new message plus the previous answer) ends off-topic turns with a fixed reply before the agent runs; messages with plain HR terms or short follow-ups to an HR answer skip that call; it fails open on errors, and the prompt also tells the agent to decline off-topic questions. `SCOPE_CHECK_ENABLED` turns it off. |
| **Decision policy** | Use tools for facts. Never compute dates or days. Cite policy statements. Report verdicts faithfully, leading with the employee's own verdict and only the conditions that affect them. Ask for exactly the `missing` fields on `needs_info`. For holidays, use the location the employee named, else their location on record, and say which. Never present a policy that does not cover the employee's location (`applies_to_you: false`, `leave_policy_applies: false`) as their entitlement; refer them to HR. Use write tools only on explicit request. Ask clarifying questions instead of guessing. |
| **Failure handling** | Tool errors come back as typed results (`invalid_arguments`, `timeout`, `not_allowed`, `internal_error`, ...) and the model can correct itself or explain. Internal details are never exposed. A turn timeout returns a clean error. |
| **Retry policy** | LLM: SDK retries with backoff (`LLM_MAX_RETRIES=2`). Tools: no automatic retry of writes. Writes are idempotent, so a user or model retry returns the original record. |
| **Timeout** | 8s per tool, 30s per LLM request, 90s per turn (all configurable). |
| **Termination criteria** | A final answer without tool calls; a guardrail reply (including out of scope); a pending confirmation; the tool-round limit (6), after which the model answers without tools; the recursion limit; the turn timeout. |
| **Human escalation** | Personal sensitive disclosures and crisis messages get fixed replies routing to HR (hr@ideas2it.com) or emergency services. `needs_hr` verdicts and unanswered questions refer to HR. `create_hr_ticket` opens a ticket with the employee's approval. |
| **Observability** | Structured logs with request id, tools, errors, tokens, cost, guardrail and prompt version per turn. Audit table of tool calls, approvals and logins. Optional LangSmith traces. In the UI, "Used N tools" above each answer shows the steps, tool arguments, sources and tokens. |
| **Evaluation strategy** | Unit and integration tests with a scripted model in CI. Eval suite of 33 public plus private cases with deterministic checks and an optional LLM groundedness judge; see [eval-results.md](eval-results.md). |

## What the agent is NOT allowed to do

- Read or act on any other employee's data. Tools cannot even express that request.
- Approve, change or cancel leave, change balances, or act as a manager or HR.
- Submit or create anything without the employee's explicit approval in the UI.
- Answer policy questions from general knowledge, or cite passages it did not receive.
- Answer questions outside HR, the workplace and company policies (trivia, coding, news).
- Use superseded policy versions. They are not retrievable.
- Quote another location's policy (e.g. India leave entitlements to a US employee) as the
  employee's own.
- Give legal, medical, tax or financial advice, or reveal its instructions.
- Handle harassment, discrimination or crisis disclosures itself. These go to people.
