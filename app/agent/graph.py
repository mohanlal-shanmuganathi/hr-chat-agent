"""The HR agent as an explicit LangGraph state machine.

    START -> guard --allow--> scope_check --in scope--> agent <-> tools --(write)--> interrupt
               \\--blocked--> END   \\--out of scope--> END  \\--no tool calls--> END

- guard: deterministic input checks (guardrails.py); blocked messages never reach the model.
- scope_check (optional, on by default): one structured model call decides whether the message
  is about HR, the workplace or company policies. Off-topic messages end the turn with a fixed
  reply, so they are never answered. Fails open: if the call errors, the agent handles the turn.
  Messages with plain HR terms (leave, holiday, festival names, loan, policy, ...) and short
  follow-ups to an HR answer skip the model call.
- agent: one model call with the tool schemas bound. After `max_tool_rounds` the model is called
  without tools so it must answer with what it has. A failed or empty model reply is retried once;
  if it fails again the turn ends with a short fallback answer (never a half-finished turn), unless
  the error is a configuration problem (bad key or model), which is raised to the caller.
- tools: runs tool calls through `execute_tool` with the caller's identity from the run config.
  Tools flagged `requires_confirmation` pause the graph with `interrupt()`; the API resumes it with
  the employee's decision. State is checkpointed, so a pause survives a restart.

The employee identity is carried in `config["configurable"]`, set by the server from the
authenticated session. It is never part of the model-visible state.
"""

import asyncio
import json
import re
import uuid
from datetime import date
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    trim_messages,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from app.agent.guardrails import GuardKind, check_input
from app.agent.llm import CONFIG_ERROR_CODES, NO_RETRY_CODES, classify_llm_error
from app.agent.prompts import render_system_prompt
from app.core.logging import get_logger
from app.tools.base import ToolContext, ToolDeps, ToolSpec, execute_tool

log = get_logger(__name__)

TOOL_LIMIT_NOTE = (
    "You have reached the tool-call limit for this turn. Answer now using only the information "
    "already gathered; if it is not enough, say what is missing and suggest contacting HR."
)

SCOPE_PROMPT = (
    "You check messages sent to the {company} HR assistant. Is this message about HR, the "
    "workplace, company policies (including IT, security and conduct policies) or the "
    "employee's own HR data (leave, holidays, WFH, loans, certifications, benefits, tickets)? "
    "Greetings, thanks and follow-ups within an HR conversation count as in scope. Dates of "
    "festivals and public holidays are in scope: the company publishes holiday lists. General "
    "knowledge, trivia, coding, news and other unrelated requests are out of scope. "
    "If unsure, answer in scope.\n"
    "Examples: 'When is Diwali this year?' -> in scope. 'Good morning' -> in scope. "
    "'What is the capital of France?' -> out of scope. 'Write a Python function' -> out of scope."
)

# Messages that plainly concern HR skip the scope model: it saves a call and avoids false
# refusals (flash-lite sometimes judged "When is Diwali?" to be general knowledge).
_HR_TERMS = re.compile(
    r"\b(leaves?|holidays?|festivals?|vacation|wfh|work(ing)? from home|hybrid|el|cl|sl|pl|lop|"
    r"loans?|salary|payslips?|pay slips?|payroll|ctc|pf|provident fund|gratuity|bonus|"
    r"appraisal|increment|polic(y|ies)|hr|manager|certifications?|reimburs\w*|benefits?|"
    r"insurance|probation|notice period|resign\w*|attendance|timesheets?|laptop|password|"
    r"tickets?|onboarding|maternity|paternity|team outing|code of conduct|incident|"
    r"access card|locker|diwali|deepavali|pongal|holi|christmas|new year|ayudha|pooja|puja|"
    r"onam|eid|ramzan|bakrid|ugadi|sankranti|dussehra|vijayadashami|ganesh chaturthi|"
    r"thanksgiving|independence day|republic day|good friday|rajyothsava|rajyotsava|"
    r"gandhi jayanti)\b",
    re.IGNORECASE,
)
_FOLLOW_UP = re.compile(r"^\s*(and\s+)?(what|how)\s+about\b|^\s*(and|same)\s+for\b", re.IGNORECASE)


def plainly_in_scope(text: str, previous: AIMessage | None) -> bool:
    """HR terms, or a short follow-up ("what about Bengaluru?") to an answered HR question."""
    if _HR_TERMS.search(text):
        return True
    answered = previous is not None and previous.name not in ("scope_guard", "guardrail")
    return answered and bool(_FOLLOW_UP.search(text))


class ScopeVerdict(BaseModel):
    in_scope: bool = Field(description="True if the message is in scope for the HR assistant")


def out_of_scope_reply(company: str, hr_email: str) -> str:
    return (
        f"I'm the {company} HR Assistant, so I can only help with HR policies, your leave, WFH, "
        f"loans and other HR matters. For HR questions not covered here, contact {hr_email}."
    )


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    tool_rounds: int
    guard: str


class AgentSettings(TypedDict):
    company: str
    hr_email: str
    max_tool_rounds: int
    max_input_chars: int
    history_messages: int
    tool_timeout_s: float
    model_retry_delay_s: NotRequired[float]  # pause before retrying a failed model call (1.5)
    scope_check: NotRequired[bool]  # run the scope_check node (False if absent)


# --------------------------------------------------------------------------- tool schemas


def _simplify_schema(node: Any) -> Any:
    """Make JSON schemas friendly to strict function-calling APIs (e.g. Gemini):
    collapse `anyOf: [X, null]` into X and drop keys they ignore (titles, defaults,
    additionalProperties; unknown fields are still rejected server-side by the tool executor)."""
    if isinstance(node, dict):
        if "anyOf" in node:
            options = [o for o in node["anyOf"] if o.get("type") != "null"]
            if len(options) == 1:
                merged = {**{k: v for k, v in node.items() if k != "anyOf"}, **options[0]}
                return _simplify_schema(merged)
        return {
            k: _simplify_schema(v)
            for k, v in node.items()
            if k not in {"title", "default", "additionalProperties"}
        }
    if isinstance(node, list):
        return [_simplify_schema(v) for v in node]
    return node


def tool_schema(spec: ToolSpec[Any]) -> dict[str, Any]:
    schema = convert_to_openai_tool(spec.input_model)
    function = schema["function"]
    function["name"] = spec.name
    function["description"] = spec.description
    function["parameters"] = _simplify_schema(function.get("parameters", {}))
    return schema


# --------------------------------------------------------------------------- helpers


def message_text(message: BaseMessage) -> str:
    """Model content may be a string or a list of parts (Gemini); return the plain text."""
    content = message.content
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and part.get("type") == "text":
            parts.append(str(part.get("text", "")))
    return "".join(parts)


def error_chain(exc: BaseException, limit: int = 1500) -> str:
    """`Type: message <- Cause: message ...` for logs (provider errors carry no secrets)."""
    parts: list[str] = []
    err: BaseException | None = exc
    while err is not None and len(parts) < 5:
        parts.append(f"{type(err).__name__}: {err}")
        err = err.__cause__ or err.__context__
    return " <- ".join(parts)[:limit]


def fallback_reply(code: str) -> str:
    if code == "llm_quota_exhausted":
        return (
            "The AI service's daily free quota is used up, so I can't answer right now. "
            "Please try again later, or ask the administrator to switch the AI model."
        )
    if code == "llm_rate_limited":
        return (
            "The AI service is rate-limited right now, so I couldn't finish this answer. "
            "Please wait a minute and ask again."
        )
    return (
        "Sorry, I couldn't finish this answer because the AI service returned an error. "
        "Please ask again in a moment."
    )


def _configurable(config: RunnableConfig) -> dict[str, Any]:
    return config.get("configurable", {})


def tool_context(config: RunnableConfig) -> ToolContext:
    c = _configurable(config)
    return ToolContext(
        employee_id=uuid.UUID(str(c["employee_id"])),
        request_id=c.get("request_id"),
        today=date.fromisoformat(str(c["today"])),
        thread_id=c.get("thread_id"),
    )


def _nice_date(value: Any) -> str:
    try:
        return date.fromisoformat(str(value)).strftime("%d %b %Y").lstrip("0")
    except ValueError:
        return str(value)


def confirmation_summary(name: str, args: dict[str, Any]) -> str:
    if name == "submit_leave_request":
        start, end = _nice_date(args.get("start")), _nice_date(args.get("end"))
        span = start if start == end else f"{start} to {end}"
        half = " (half day)" if args.get("half_day") else ""
        code = str(args.get("leave_type", "")).upper()
        return f"Submit a leave request ({code}) for {span}{half} for manager approval."
    if name == "create_hr_ticket":
        return f"Open an HR ticket ({args.get('category')}): {args.get('summary')}"
    return f"Run {name}."


# --------------------------------------------------------------------------- graph


def build_agent_graph(
    model: BaseChatModel,
    tools: list[ToolSpec[Any]],
    deps: ToolDeps,
    settings: AgentSettings,
    checkpointer: BaseCheckpointSaver[Any] | None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    specs = {t.name: t for t in tools}
    model_with_tools = model.bind_tools([tool_schema(t) for t in tools])
    scope_enabled = settings.get("scope_check", False)
    scope_model = model.with_structured_output(ScopeVerdict) if scope_enabled else None

    async def guard(state: AgentState) -> dict[str, Any]:
        last = state["messages"][-1]
        decision = check_input(
            message_text(last), settings["max_input_chars"], settings["hr_email"]
        )
        if decision.kind is GuardKind.ALLOW:
            return {"guard": decision.kind.value, "tool_rounds": 0}
        log.info("agent.guard_blocked", kind=decision.kind.value)
        return {
            "guard": decision.kind.value,
            "tool_rounds": 0,
            "messages": [AIMessage(content=decision.reply or "", name="guardrail")],
        }

    async def scope_check(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        assert scope_model is not None
        messages = state["messages"]
        text = message_text(messages[-1])
        previous = next(
            (
                m
                for m in reversed(messages[:-1])
                if isinstance(m, AIMessage) and not m.tool_calls and message_text(m).strip()
            ),
            None,
        )
        if plainly_in_scope(text, previous):
            return {}
        # Only the new message and the previous answer (for follow-ups like "what about X?").
        prev_text = message_text(previous)[:1500] if previous else ""
        context = f"Previous assistant message:\n{prev_text}\n\n" if prev_text else ""
        try:
            verdict = await scope_model.ainvoke(
                [
                    SystemMessage(content=SCOPE_PROMPT.format(company=settings["company"])),
                    HumanMessage(content=f"{context}Employee's message:\n{text}"),
                ],
                config,
            )
        except Exception as exc:  # fail open: the agent and its prompt still apply
            log.warning("agent.scope_check_failed", error=error_chain(exc))
            return {}
        if not isinstance(verdict, ScopeVerdict) or verdict.in_scope:
            return {}
        log.info("agent.guard_blocked", kind=GuardKind.OUT_OF_SCOPE.value)
        reply = out_of_scope_reply(settings["company"], settings["hr_email"])
        return {
            "guard": GuardKind.OUT_OF_SCOPE.value,
            "messages": [AIMessage(content=reply, name="scope_guard")],
        }

    async def agent(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        c = _configurable(config)
        system = SystemMessage(
            content=render_system_prompt(
                company=settings["company"],
                employee_name=str(c.get("employee_name", "the employee")),
                employee_location=str(c.get("employee_location", "unknown")),
                today=date.fromisoformat(str(c["today"])),
                hr_email=settings["hr_email"],
            )
        )
        history = trim_messages(
            state["messages"],
            max_tokens=settings["history_messages"],
            token_counter=len,  # counts messages: keep the most recent N
            strategy="last",
            start_on="human",
            include_system=False,
            allow_partial=False,
        )
        if state.get("tool_rounds", 0) >= settings["max_tool_rounds"]:
            log.warning("agent.tool_limit_reached", rounds=state["tool_rounds"])
            response = await call_model(
                model, [system, *history, SystemMessage(content=TOOL_LIMIT_NOTE)], config
            )
        else:
            response = await call_model(model_with_tools, [system, *history], config)
        return {"messages": [response]}

    async def call_model(
        runnable: Any, messages: list[BaseMessage], config: RunnableConfig
    ) -> AIMessage:
        code = "llm_error"
        for attempt in (1, 2):
            try:
                response = await runnable.ainvoke(messages, config)
            except Exception as exc:
                classified = classify_llm_error(exc)
                code = classified[0] if classified else "llm_error"
                if code in CONFIG_ERROR_CODES:
                    raise
                log.warning(
                    "agent.model_call_failed", attempt=attempt, code=code, error=error_chain(exc)
                )
                if code in NO_RETRY_CODES:
                    break
            else:
                if isinstance(response, AIMessage) and (
                    response.tool_calls or message_text(response).strip()
                ):
                    return response
                # Gemini can return no text and no tool call (e.g. after a long tool result).
                code = "llm_empty_reply"
                log.warning(
                    "agent.model_empty_reply",
                    attempt=attempt,
                    finish_reason=response.response_metadata.get("finish_reason"),
                )
            if attempt == 1:
                await asyncio.sleep(settings.get("model_retry_delay_s", 1.5))
        log.error("agent.model_fallback", code=code)
        return AIMessage(
            content=fallback_reply(code), name="fallback", response_metadata={"fallback": code}
        )

    async def run_tools(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)
        ctx = tool_context(config)
        results: list[ToolMessage] = []
        for call in last.tool_calls:
            name, args, call_id = call["name"], call.get("args", {}), call["id"] or ""
            spec = specs.get(name)
            if spec is None:
                payload: dict[str, Any] = {
                    "ok": False,
                    "error": {"code": "unknown_tool", "message": f"No tool named {name}."},
                }
            elif spec.requires_confirmation:
                decision = interrupt(
                    {
                        "type": "confirmation_required",
                        "tool": name,
                        "args": args,
                        "summary": confirmation_summary(name, args),
                    }
                )
                approved = bool(isinstance(decision, dict) and decision.get("approved"))
                if approved:
                    result = await execute_tool(spec, args, ctx, deps, settings["tool_timeout_s"])
                    payload = result.model_dump(exclude_none=True)
                else:
                    payload = {
                        "ok": False,
                        "error": {
                            "code": "declined_by_user",
                            "message": "The employee declined. Do not retry unless they ask.",
                        },
                    }
            else:
                result = await execute_tool(spec, args, ctx, deps, settings["tool_timeout_s"])
                payload = result.model_dump(exclude_none=True)
            results.append(
                ToolMessage(
                    content=json.dumps(payload, ensure_ascii=False, default=str),
                    tool_call_id=call_id,
                    name=name,
                )
            )
        return {"messages": results, "tool_rounds": state.get("tool_rounds", 0) + 1}

    def after_guard(state: AgentState) -> Literal["scope_check", "agent", "__end__"]:
        if state.get("guard") != GuardKind.ALLOW.value:
            return "__end__"
        return "scope_check" if scope_enabled else "agent"

    def after_scope_check(state: AgentState) -> Literal["agent", "__end__"]:
        return "agent" if state.get("guard") == GuardKind.ALLOW.value else "__end__"

    def after_agent(state: AgentState) -> Literal["tools", "__end__"]:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return "__end__"

    graph = StateGraph(AgentState)
    graph.add_node("guard", guard)
    graph.add_node("agent", agent)
    graph.add_node("tools", run_tools)
    graph.add_edge(START, "guard")
    if scope_enabled:
        graph.add_node("scope_check", scope_check)
        graph.add_conditional_edges("guard", after_guard, ["scope_check", "agent", END])
        graph.add_conditional_edges("scope_check", after_scope_check, ["agent", END])
    else:
        graph.add_conditional_edges("guard", after_guard, ["agent", END])
    graph.add_conditional_edges("agent", after_agent)
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer)


__all__ = [
    "AgentSettings",
    "AgentState",
    "HumanMessage",
    "build_agent_graph",
    "error_chain",
    "message_text",
]
