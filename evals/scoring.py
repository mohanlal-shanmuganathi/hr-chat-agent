"""Evaluation cases and deterministic scoring.

A case is one conversation (one or more user turns) for a fictional employee, with expectations
checked mechanically against the agent's turn results: which tools ran, with which arguments,
what the answer contains, which policies were cited, whether a guardrail fired, and whether a
write action paused for confirmation. Deterministic checks first; an optional LLM judge adds a
groundedness check for answers based on policy text.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tools_all: list[str] = []  # every one of these tools must run (any order)
    tools_any: list[str] = []  # at least one of these must run
    tools_none: list[str] = []  # none of these may run
    tool_args: dict[str, dict[str, Any]] = {}  # tool -> expected argument values (subset match)
    status: str | None = None  # completed | awaiting_confirmation (final turn, before approval)
    guard: str | None = None  # allow | sensitive | injection | crisis | out_of_scope
    answer_contains_all: list[str] = []
    answer_contains_any: list[str] = []
    answer_not_contains: list[str] = []
    citation_contains_any: list[str] = []
    max_tool_calls: int | None = None
    grounded: bool = False  # ask the LLM judge whether the answer is supported by tool output


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    employee: str  # fictional employee email
    turns: list[str] = Field(min_length=1)
    approve: bool | None = None  # how to answer a confirmation request, if one comes
    expect: Expect
    notes: str | None = None


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class CaseOutcome:
    case: EvalCase
    checks: list[Check] = field(default_factory=list)
    answer: str = ""
    tools: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    error: str | None = None
    note: str | None = None  # e.g. "retried after rate limit"

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.ok for c in self.checks)


def _norm(text: str) -> str:
    """Lowercase, collapse whitespace, and drop Markdown emphasis so "**2** days" matches."""
    text = re.sub(r"[*_`]", "", text.replace("\u2019", "'"))
    return re.sub(r"\s+", " ", text).strip().lower()


def _arg_matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, str) and isinstance(actual, str):
        return _norm(expected) in _norm(actual)
    return bool(expected == actual)


def score(
    case: EvalCase,
    *,
    answer: str,
    tool_uses: list[dict[str, Any]],  # [{"name", "ok", "args"}] across all turns
    citations: list[str],
    status_before_approval: str,
    guard: str | None,
) -> list[Check]:
    e = case.expect
    checks: list[Check] = []
    names = [t["name"] for t in tool_uses]
    text = _norm(answer)

    for tool in e.tools_all:
        checks.append(Check(f"tool:{tool}", tool in names, f"ran {names}"))
    if e.tools_any:
        checks.append(
            Check(
                f"any_tool:{'|'.join(e.tools_any)}",
                any(t in names for t in e.tools_any),
                f"ran {names}",
            )
        )
    for tool in e.tools_none:
        checks.append(Check(f"no_tool:{tool}", tool not in names, f"ran {names}"))
    for tool, expected_args in e.tool_args.items():
        calls = [t.get("args", {}) for t in tool_uses if t["name"] == tool]
        ok = any(
            all(_arg_matches(v, call.get(k)) for k, v in expected_args.items()) for call in calls
        )
        checks.append(Check(f"args:{tool}", ok, f"expected {expected_args}, got {calls}"))
    if e.status:
        checks.append(Check("status", status_before_approval == e.status, status_before_approval))
    if e.guard:
        checks.append(Check("guard", (guard or "allow") == e.guard, str(guard)))
    for phrase in e.answer_contains_all:
        checks.append(Check(f"contains:{phrase}", _norm(phrase) in text))
    if e.answer_contains_any:
        checks.append(
            Check(
                f"contains_any:{'|'.join(e.answer_contains_any)}",
                any(_norm(p) in text for p in e.answer_contains_any),
            )
        )
    for phrase in e.answer_not_contains:
        checks.append(Check(f"not_contains:{phrase}", _norm(phrase) not in text))
    if e.citation_contains_any:
        cited = " ".join(citations).lower()
        checks.append(
            Check(
                f"cites:{'|'.join(e.citation_contains_any)}",
                any(p.lower() in cited for p in e.citation_contains_any),
                f"citations {citations}",
            )
        )
    if e.max_tool_calls is not None:
        checks.append(
            Check("max_tool_calls", len(names) <= e.max_tool_calls, f"{len(names)} calls")
        )
    return checks
