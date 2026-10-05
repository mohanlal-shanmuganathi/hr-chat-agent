"""Chat application service: runs a turn of the agent for an authenticated employee.

Responsibilities kept out of the graph:
- thread ownership: the checkpoint key is namespaced by employee, so one employee can never load
  another's conversation even with a guessed thread id;
- per-turn timeout;
- leaving the conversation consistent when a turn fails, so the next turn does not inherit a
  half-finished one (an unanswered question or tool calls without results);
- resuming a paused write action with the employee's decision;
- assembling a turn result (answer, citations, tools used, pending confirmation, token usage).
"""

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.agent.graph import error_chain, fallback_reply, message_text
from app.agent.llm import classify_llm_error
from app.agent.prompts import SYSTEM_PROMPT_VERSION
from app.core.logging import get_logger

log = get_logger(__name__)


TOOL_LABELS = {
    "search_hr_policies": "Searching policy documents",
    "get_my_profile": "Reading your profile",
    "get_my_leave_balances": "Checking your leave balances",
    "get_my_leave_history": "Reading your leave history",
    "get_holidays": "Looking up holidays",
    "calculate_leave_days": "Counting working days",
    "check_leave_eligibility": "Checking leave eligibility rules",
    "check_wfh_eligibility": "Checking WFH eligibility",
    "check_staff_loan_eligibility": "Checking staff loan eligibility",
    "check_certification_reimbursement": "Checking certification reimbursement rules",
    "submit_leave_request": "Submitting your leave request",
    "create_hr_ticket": "Opening an HR ticket",
}


class ChatError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class EmployeeIdentity:
    employee_id: uuid.UUID
    name: str
    location: str


@dataclass
class ToolUse:
    name: str
    ok: bool
    error_code: str | None = None
    args: dict[str, Any] = field(default_factory=dict)  # what the model asked for (explainability)


@dataclass
class TurnResult:
    thread_id: str
    status: str  # "completed" | "awaiting_confirmation"
    answer: str | None
    pending_action: dict[str, Any] | None
    citations: list[str] = field(default_factory=list)
    tools_used: list[ToolUse] = field(default_factory=list)
    guard: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    prompt_version: str = SYSTEM_PROMPT_VERSION
    fallback: str | None = None  # error code when the answer is a fallback, not the model's


class ChatService:
    def __init__(
        self,
        graph: CompiledStateGraph[Any, Any, Any, Any],
        turn_timeout_s: float,
        max_tool_rounds: int,
        cost_per_mtok: tuple[float, float] = (0.0, 0.0),  # (input, output) USD per 1M tokens
    ) -> None:
        self._graph = graph
        self._cost_per_mtok = cost_per_mtok
        self._timeout = turn_timeout_s
        self._recursion_limit = max_tool_rounds * 2 + 6

    @staticmethod
    def _checkpoint_thread(employee_id: uuid.UUID, thread_id: str) -> str:
        return f"{employee_id}:{thread_id}"

    def _config(
        self, who: EmployeeIdentity, thread_id: str, today: date, request_id: str | None
    ) -> RunnableConfig:
        return {
            "configurable": {
                "thread_id": self._checkpoint_thread(who.employee_id, thread_id),
                "employee_id": str(who.employee_id),
                "employee_name": who.name,
                "employee_location": who.location,
                "today": today.isoformat(),
                "request_id": request_id,
            },
            "recursion_limit": self._recursion_limit,
            "run_name": "hr_agent_turn",
            "tags": ["hr-chat-agent", SYSTEM_PROMPT_VERSION],
            "metadata": {
                "prompt_version": SYSTEM_PROMPT_VERSION,
                "employee_ref": hashlib.sha256(str(who.employee_id).encode()).hexdigest()[:12],
                "request_id": request_id,
            },
        }

    async def _pending(self, config: RunnableConfig) -> dict[str, Any] | None:
        state = await self._graph.aget_state(config)
        for task in state.tasks:
            for intr in task.interrupts:
                return dict(intr.value)
        return None

    async def _run(self, payload: Any, config: RunnableConfig) -> None:
        try:
            await asyncio.wait_for(self._graph.ainvoke(payload, config), timeout=self._timeout)
        except Exception as exc:
            error = await self._failed(exc, config)
            if error is exc:
                raise
            raise error from exc

    async def send(
        self,
        who: EmployeeIdentity,
        thread_id: str,
        text: str,
        today: date,
        request_id: str | None = None,
    ) -> TurnResult:
        config = self._config(who, thread_id, today, request_id)
        if await self._pending(config):
            raise ChatError(
                "confirmation_pending",
                "Please approve or reject the pending action before sending a new message.",
            )
        before = len((await self._graph.aget_state(config)).values.get("messages", []))
        await self._run({"messages": [HumanMessage(content=text)]}, config)
        return await self._result(config, thread_id, before)

    async def resume(
        self,
        who: EmployeeIdentity,
        thread_id: str,
        approved: bool,
        today: date,
        request_id: str | None = None,
    ) -> TurnResult:
        config = self._config(who, thread_id, today, request_id)
        if not await self._pending(config):
            raise ChatError("nothing_pending", "There is no action waiting for confirmation.")
        state = await self._graph.aget_state(config)
        messages = state.values.get("messages", [])
        turn_start = max(
            (i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=0
        )
        log.info("agent.confirmation_decision", approved=approved)
        await self._run(Command(resume={"approved": approved}), config)
        return await self._result(config, thread_id, turn_start)

    async def stream_send(
        self,
        who: EmployeeIdentity,
        thread_id: str,
        text: str,
        today: date,
        request_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Like `send`, but yields progress events and finally {"type": "result", ...}."""
        config = self._config(who, thread_id, today, request_id)
        if await self._pending(config):
            raise ChatError(
                "confirmation_pending",
                "Please approve or reject the pending action before sending a new message.",
            )
        before = len((await self._graph.aget_state(config)).values.get("messages", []))
        async for event in self._stream({"messages": [HumanMessage(content=text)]}, config):
            yield event
        yield {"type": "result", **asdict(await self._result(config, thread_id, before))}

    async def stream_resume(
        self,
        who: EmployeeIdentity,
        thread_id: str,
        approved: bool,
        today: date,
        request_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        config = self._config(who, thread_id, today, request_id)
        if not await self._pending(config):
            raise ChatError("nothing_pending", "There is no action waiting for confirmation.")
        messages = (await self._graph.aget_state(config)).values.get("messages", [])
        turn_start = max(
            (i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=0
        )
        log.info("agent.confirmation_decision", approved=approved)
        async for event in self._stream(Command(resume={"approved": approved}), config):
            yield event
        yield {"type": "result", **asdict(await self._result(config, thread_id, turn_start))}

    async def _stream(self, payload: Any, config: RunnableConfig) -> AsyncIterator[dict[str, Any]]:
        try:
            async with asyncio.timeout(self._timeout):
                async for update in self._graph.astream(payload, config, stream_mode="updates"):
                    for node, change in update.items():
                        for event in _progress_events(node, change):
                            yield event
        except Exception as exc:
            error = await self._failed(exc, config)
            if error is exc:
                raise
            raise error from exc

    async def _failed(self, exc: Exception, config: RunnableConfig) -> Exception:
        """Log a failed turn, repair the thread, and return the error to raise to the caller."""
        error: Exception
        if isinstance(exc, TimeoutError):
            log.error("agent.turn_timeout", timeout_s=self._timeout)
            code = "timeout"
            error = ChatError(code, "The assistant took too long. Please try again.")
        elif isinstance(exc, ChatError):
            code, error = exc.code, exc
        elif classified := classify_llm_error(exc):
            log.error("agent.llm_error", code=classified[0], error=error_chain(exc))
            code = classified[0]
            error = ChatError(*classified)
        else:
            log.exception("agent.turn_failed", error=error_chain(exc))
            code, error = "internal_error", exc
        await self._repair(config, code)
        return error

    async def _repair(self, config: RunnableConfig, code: str) -> None:
        """End a failed turn with a fallback answer so the thread stays consistent.

        Otherwise the checkpoint keeps the unanswered question (and any tool calls without
        results), and the model answers it again on the next, unrelated question.
        """
        try:
            state = await self._graph.aget_state(config)
            if any(task.interrupts for task in state.tasks):
                return  # a paused confirmation is a valid state
            messages = state.values.get("messages", [])
            last = messages[-1] if messages else None
            if last is None or (isinstance(last, AIMessage) and not last.tool_calls):
                return
            closing: list[Any] = []
            if isinstance(last, AIMessage):
                closing += [
                    ToolMessage(
                        content=json.dumps(
                            {"ok": False, "error": {"code": "not_run", "message": "Turn failed."}}
                        ),
                        tool_call_id=call["id"] or "",
                        name=call["name"],
                    )
                    for call in last.tool_calls
                ]
            closing.append(
                AIMessage(
                    content=fallback_reply(code),
                    name="fallback",
                    response_metadata={"fallback": code},
                )
            )
            await self._graph.aupdate_state(config, {"messages": closing}, as_node="agent")
            log.info("agent.turn_repaired", code=code)
        except Exception:
            log.exception("agent.turn_repair_failed")

    async def transcript(self, who: EmployeeIdentity, thread_id: str) -> list[dict[str, Any]]:
        """Full turn record incl. tool payloads (for evaluation and debugging, not the UI)."""
        config = self._config(who, thread_id, date.today(), None)
        state = await self._graph.aget_state(config)
        out: list[dict[str, Any]] = []
        for m in state.values.get("messages", []):
            if isinstance(m, HumanMessage):
                out.append({"role": "user", "content": message_text(m)})
            elif isinstance(m, ToolMessage):
                out.append({"role": "tool", "name": m.name, "content": message_text(m)})
            elif isinstance(m, AIMessage):
                out.append(
                    {
                        "role": "assistant",
                        "content": message_text(m),
                        "tool_calls": [c["name"] for c in m.tool_calls],
                    }
                )
        return out

    async def history(self, who: EmployeeIdentity, thread_id: str) -> list[dict[str, str]]:
        config = self._config(who, thread_id, date.today(), None)
        state = await self._graph.aget_state(config)
        out = []
        for m in state.values.get("messages", []):
            if isinstance(m, HumanMessage):
                out.append({"role": "user", "content": message_text(m)})
            elif isinstance(m, AIMessage) and not m.tool_calls and message_text(m):
                out.append({"role": "assistant", "content": message_text(m)})
        return out

    async def _result(self, config: RunnableConfig, thread_id: str, start: int) -> TurnResult:
        state = await self._graph.aget_state(config)
        messages = state.values.get("messages", [])[start:]
        pending = await self._pending(config)

        citations: list[str] = []
        tools_used: list[ToolUse] = []
        input_tokens = output_tokens = 0
        call_args = {
            call["id"]: call.get("args", {})
            for m in messages
            if isinstance(m, AIMessage)
            for call in m.tool_calls
        }
        for m in messages:
            if isinstance(m, ToolMessage):
                try:
                    payload = json.loads(message_text(m))
                except json.JSONDecodeError:
                    payload = {}
                error = payload.get("error") or {}
                tools_used.append(
                    ToolUse(
                        m.name or "?",
                        bool(payload.get("ok")),
                        error.get("code"),
                        dict(call_args.get(m.tool_call_id, {})),
                    )
                )
                if m.name == "search_hr_policies" and payload.get("ok"):
                    for p in (payload.get("data") or {}).get("passages", []):
                        if p.get("citation") and p["citation"] not in citations:
                            citations.append(p["citation"])
            elif isinstance(m, AIMessage) and m.usage_metadata:
                input_tokens += m.usage_metadata.get("input_tokens", 0)
                output_tokens += m.usage_metadata.get("output_tokens", 0)

        last = messages[-1] if messages else None
        fallback = (
            last.response_metadata.get("fallback")
            if isinstance(last, AIMessage) and last.name == "fallback"
            else None
        )
        answer = (
            message_text(last)
            if isinstance(last, AIMessage) and not last.tool_calls and not pending
            else None
        )
        result = TurnResult(
            thread_id=thread_id,
            status="awaiting_confirmation" if pending else "completed",
            answer=answer,
            pending_action=pending,
            citations=citations,
            tools_used=tools_used,
            guard=state.values.get("guard"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=round(
                (input_tokens * self._cost_per_mtok[0] + output_tokens * self._cost_per_mtok[1])
                / 1_000_000,
                6,
            ),
            fallback=fallback,
        )
        log.info(
            "agent.turn_completed",
            status=result.status,
            guard=result.guard,
            tools=[t.name for t in tools_used],
            tool_errors=[t.error_code for t in tools_used if not t.ok],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=result.estimated_cost_usd,
            prompt_version=SYSTEM_PROMPT_VERSION,
            fallback=fallback,
            thread=thread_id,
        )
        return result


def _progress_events(node: str, change: Any) -> list[dict[str, Any]]:
    if not isinstance(change, dict):
        return []
    events: list[dict[str, Any]] = []
    for m in change.get("messages", []):
        if node == "agent" and isinstance(m, AIMessage) and m.tool_calls:
            for call in m.tool_calls:
                events.append(
                    {
                        "type": "tool_call",
                        "tool": call["name"],
                        "label": TOOL_LABELS.get(call["name"], call["name"]),
                    }
                )
        elif node == "tools" and isinstance(m, ToolMessage):
            try:
                payload = json.loads(message_text(m))
            except json.JSONDecodeError:
                payload = {}
            events.append(
                {
                    "type": "tool_result",
                    "tool": m.name,
                    "ok": bool(payload.get("ok")),
                    "error_code": (payload.get("error") or {}).get("code"),
                }
            )
    return events
