# ADR-005: Identity injection and an HR data provider port (mock today)

**Status:** accepted

## Context
Employees must only see their own data. The employee portal (iAssistant) has no API available to
this project.

## Decision
1. **Identity injection.** The authenticated employee id travels from the session into the graph's
   run config and then into every tool as `ToolContext`. Tool argument schemas contain **no
   employee identifier** and reject unknown fields, so the model cannot ask for anyone else's data.
2. **Provider port.** Tools depend on `HRDataProvider` (`app/hr/provider.py`). Today
   `DbHRDataProvider` reads a mock database of fictional employees. An iAssistant API client
   would implement the same protocol, using a service credential and never a user's portal login.
3. **Demo provisioning** (mock only, off by default) copies a fictional profile for a real SSO
   user on first login, so the demo can show "my data".

## Consequences
Authorisation is enforced by code, not by the prompt (tested). Moving to real HR data is a
provider swap.
