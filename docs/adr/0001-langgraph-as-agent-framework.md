# ADR-001: LangGraph as the agent framework

**Status:** accepted

## Context
The assessment asks for an agentic framework. The agent needs tool calling, a bounded loop,
human approval before side effects, and conversation state that survives restarts.

## Options
| Option | Strengths | Weaknesses |
|---|---|---|
| **LangGraph** | The control flow is an explicit graph. `interrupt()` provides human-in-the-loop. The Postgres checkpointer persists state. Mature and widely used. | Pulls in `langchain-core`. More concepts to learn. |
| Pydantic AI | Light, type-safe, fits FastAPI. | Persistence and human-in-the-loop would be hand-built. |
| OpenAI Agents SDK | Simple handoffs and guardrails. | Strongest with OpenAI models, so more lock-in. |
| Plain Python loop | No dependency, full control. | We would re-implement checkpointing and interrupts. |

## Decision
LangGraph. The coupling is contained: tools are framework-free functions (`app/tools`), the rules
engine and HR provider know nothing about LangChain, and only `app/agent/` imports it. Replacing
the framework means rewriting `graph.py` and `service.py`, not the domain.
