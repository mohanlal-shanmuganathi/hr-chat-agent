# ADR-006: Gemini (free tier) behind a provider factory

**Status:** accepted

## Context
The project must cost nothing. Consumer subscriptions (Claude Pro, ChatGPT Go) do not include API
access.

## Options
| Option | Cost | Tool calling | Privacy |
|---|---|---|---|
| **Gemini API free tier** | free, rate-limited | strong | free-tier data may be used by Google |
| Groq free tier (open models) | free, rate-limited | good | check provider terms |
| Ollama (local) | free | weaker for small models | stays on the laptop |

## Decision
Gemini Flash by default (`LLM_PROVIDER`, `LLM_MODEL`). `app/agent/llm.py` is the only
provider-specific code. Tool schemas are simplified to the JSON-schema subset strict
function-calling APIs accept, checked against Gemini's converter. The free-tier privacy
trade-off is documented in the README and architecture doc.

## Consequences
Switching provider is configuration plus one package. Behaviour differs between models, so re-run
the evaluation suite after any switch.
