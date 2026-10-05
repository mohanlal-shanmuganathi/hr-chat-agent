"""Tool layer against the seeded mock HR database (fictional employees, synthetic holidays)."""

import asyncio
import uuid
from datetime import date
from typing import Any

from app.observability.audit import InMemoryAuditSink
from app.tools.base import ToolContext, ToolDeps, ToolInput, ToolResult, ToolSpec, execute_tool
from app.tools.hr_tools import TOOLS_BY_NAME

TODAY = date(2026, 10, 3)


async def ctx_for(deps: ToolDeps, email: str) -> ToolContext:
    emp_id = await deps.hr.find_employee_id_by_email(email)
    assert emp_id
    return ToolContext(employee_id=emp_id, request_id="req-1", today=TODAY)


async def run(deps: ToolDeps, ctx: ToolContext, name: str, **args: Any) -> ToolResult:
    return await execute_tool(TOOLS_BY_NAME[name], args, ctx, deps, default_timeout_s=5)


async def test_model_cannot_target_another_employee(deps: ToolDeps) -> None:
    ctx = await ctx_for(deps, "priya.r@example.com")
    other = await deps.hr.find_employee_id_by_email("rohan.k@example.com")
    res = await run(deps, ctx, "get_my_leave_balances", employee_id=str(other))
    assert not res.ok and res.error and res.error["code"] == "invalid_arguments"

    profile = await run(deps, ctx, "get_my_profile")
    assert profile.ok and profile.data and profile.data["name"] == "Priya R"


async def test_balances_and_missing_records(deps: ToolDeps) -> None:
    priya = await run(deps, await ctx_for(deps, "priya.r@example.com"), "get_my_leave_balances")
    assert priya.data
    cl = next(b for b in priya.data["balances"] if "(CL)" in b["type"])
    assert (cl["available"], cl["pending_approval"]) == (2.0, 2.0)

    emily = await run(
        deps, await ctx_for(deps, "emily.carter@example.com"), "get_my_leave_balances"
    )
    assert emily.data and emily.data["balances"] == []
    assert "hr@example.com" in emily.data["note"]


async def test_holidays_default_to_the_profile_location(deps: ToolDeps) -> None:
    ctx = await ctx_for(deps, "priya.r@example.com")
    own = await run(deps, ctx, "get_holidays", year=2026)
    assert own.data and own.data["location"] == "Chennai (Tamil Nadu)"
    assert own.data["location_source"] == "employee_profile"
    assert own.data["other_locations_available"] == ["Karnataka (Bengaluru)"]

    kar = await run(deps, ctx, "get_holidays", location="Bengaluru", year=2026)
    assert kar.data and [h["name"] for h in kar.data["holidays"]] == ["Fest B"]
    assert kar.data["location_source"] == "named_by_employee"

    unknown = await run(deps, ctx, "get_holidays", location="Mumbai")
    assert unknown.data and unknown.data["needs_location_confirmation"] is True

    # No holiday list for the profile location: ask instead of guessing.
    emily = await ctx_for(deps, "emily.carter@example.com")
    ask = await run(deps, emily, "get_holidays")
    assert ask.data and ask.data["needs_location_confirmation"] is True


async def test_leave_days_use_own_location_holidays(deps: ToolDeps) -> None:
    ctx = await ctx_for(deps, "priya.r@example.com")
    res = await run(deps, ctx, "calculate_leave_days", start="2026-10-16", end="2026-10-20")
    assert res.data and res.data["leave_days_needed"] == 2.0
    assert {d["reason"] for d in res.data["not_counted"]} == {"weekend", "Fest A"}

    bad = await run(deps, ctx, "calculate_leave_days", start="2026-10-20", end="2026-10-16")
    assert bad.error and bad.error["code"] == "invalid_arguments"


async def test_eligibility_tools(deps: ToolDeps) -> None:
    rahul = await run(
        deps,
        await ctx_for(deps, "rahul.p@example.com"),
        "check_leave_eligibility",
        leave_type="EL",
        start="2026-10-12",
        end="2026-10-13",
    )
    assert rahul.data and rahul.data["verdict"] == "not_eligible"

    wfh = await run(deps, await ctx_for(deps, "kavya.s@example.com"), "check_wfh_eligibility")
    assert wfh.data and wfh.data["verdict"] == "not_eligible"


async def test_submit_leave_is_rechecked_idempotent_and_audited(deps: ToolDeps) -> None:
    ctx = await ctx_for(deps, "priya.r@example.com")
    args = {"leave_type": "EL", "start": "2026-11-09", "end": "2026-11-10", "reason": "Family"}

    first = await run(deps, ctx, "submit_leave_request", **args)
    assert first.ok and first.data and first.data["status"] == "pending"
    assert first.data["already_existed"] is False

    again = await run(deps, ctx, "submit_leave_request", **args)
    assert again.data and again.data["request_id"] == first.data["request_id"]
    assert again.data["already_existed"] is True

    overlap = await run(
        deps, ctx, "submit_leave_request", leave_type="SL", start="2026-11-10", end="2026-11-10"
    )
    assert overlap.error and overlap.error["code"] == "not_allowed"

    rahul = await ctx_for(deps, "rahul.p@example.com")
    blocked = await run(
        deps, rahul, "submit_leave_request", leave_type="CL", start="2026-10-12", end="2026-10-12"
    )
    assert blocked.error and blocked.error["code"] == "not_allowed"

    audit = deps.audit
    assert isinstance(audit, InMemoryAuditSink)
    submits = [e for e in audit.events if e.name == "submit_leave_request"]
    assert len(submits) == 4
    assert submits[0].details["args"]["reason"] == "<redacted 6 chars>"
    assert {e.outcome for e in submits} == {"ok", "denied"}


async def test_ticket_is_idempotent(deps: ToolDeps) -> None:
    ctx = await ctx_for(deps, "vikram.n@example.com")
    args = {"category": "policy_clarification", "summary": "Need clarity on EL encashment."}
    first = await run(deps, ctx, "create_hr_ticket", **args)
    second = await run(deps, ctx, "create_hr_ticket", **args)
    assert first.data and second.data and first.data["ticket_id"] == second.data["ticket_id"]
    assert second.data["already_existed"] is True


async def test_search_rejects_unknown_location(deps: ToolDeps) -> None:
    ctx = await ctx_for(deps, "priya.r@example.com")
    res = await run(deps, ctx, "search_hr_policies", query="leave policy", location="Atlantis")
    assert res.error and res.error["code"] == "unknown_location"
    ok = await run(deps, ctx, "search_hr_policies", query="leave policy")
    assert ok.ok and ok.data and "note" in ok.data


async def test_executor_times_out_and_hides_internal_errors(deps: ToolDeps) -> None:
    class Empty(ToolInput):
        pass

    async def slow(args: Empty, ctx: ToolContext, d: ToolDeps) -> dict[str, Any]:
        await asyncio.sleep(1)
        return {}

    async def broken(args: Empty, ctx: ToolContext, d: ToolDeps) -> dict[str, Any]:
        raise RuntimeError("database password is hunter2")

    ctx = ToolContext(employee_id=uuid.uuid4(), request_id=None, today=TODAY)
    timed = await execute_tool(
        ToolSpec(name="slow", description="", input_model=Empty, handler=slow, timeout_s=0.05),
        {},
        ctx,
        deps,
        default_timeout_s=5,
    )
    assert timed.error and timed.error["code"] == "timeout" and timed.error["retryable"]

    failed = await execute_tool(
        ToolSpec(name="broken", description="", input_model=Empty, handler=broken),
        {},
        ctx,
        deps,
        default_timeout_s=5,
    )
    assert failed.error and failed.error["code"] == "internal_error"
    assert "hunter2" not in str(failed.error)
