# ADR-002: One agent with deterministic tools, not a multi-agent system

**Status:** accepted

## Context
The questions (policy lookups, balances, eligibility, leave requests, holidays, loans,
certifications) share one employee context and one set of tools.

## Options
- **Multi-agent** (router plus specialist agents for policy, leave, benefits): more model calls,
  more latency and cost, hand-offs that lose context, and more ways to fail. It adds no capability
  here.
- **Fixed workflow, no agent:** cannot handle open-ended questions that need several tools
  chosen at run time (profile → holidays → calculation → eligibility → policy citation).
- **One agent plus deterministic tools** (chosen).

## Decision
One tool-calling agent in an explicit graph with deterministic guard nodes. The model decides
*which* tools to call and *how to explain* results. Everything that must be correct is plain
Python: dates, working days, balances, eligibility rules, permissions, idempotency. The model never
does arithmetic, and the system prompt and tool descriptions tell it so.

## Consequences
Behaviour is testable without an LLM (rules engine and tools have unit tests), and the model's
part is small enough to evaluate with the eval suite. Splitting into agents can be reconsidered if
the domain grows, for example into payroll or recruitment with their own tool sets.
