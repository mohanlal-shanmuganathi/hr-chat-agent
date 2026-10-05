from pathlib import Path

import yaml

from app.tools.hr_tools import TOOLS_BY_NAME
from evals.run_evals import load_cases
from evals.scoring import EvalCase, Expect, score

SEED_EMAILS = {
    e["email"] for e in yaml.safe_load(Path("data/seed/employees.yaml").read_text())["employees"]
}


def case(**expect: object) -> EvalCase:
    return EvalCase(
        id="T", category="t", employee="x@example.com", turns=["q"], expect=Expect(**expect)
    )


def failed(checks: list) -> list[str]:  # type: ignore[type-arg]
    return [c.name for c in checks if not c.ok]


def test_tool_and_argument_checks() -> None:
    uses = [
        {
            "name": "check_leave_eligibility",
            "ok": True,
            "args": {"leave_type": "CL", "start": "2026-10-16"},
        },
    ]
    good = score(
        case(
            tools_all=["check_leave_eligibility"],
            tools_none=["submit_leave_request"],
            tool_args={"check_leave_eligibility": {"leave_type": "cl", "start": "2026-10-16"}},
        ),
        answer="",
        tool_uses=uses,
        citations=[],
        status_before_approval="completed",
        guard="allow",
    )
    assert failed(good) == []

    bad = score(
        case(
            tools_any=["get_holidays"], tool_args={"check_leave_eligibility": {"leave_type": "EL"}}
        ),
        answer="",
        tool_uses=uses,
        citations=[],
        status_before_approval="completed",
        guard="allow",
    )
    assert failed(bad) == ["any_tool:get_holidays", "args:check_leave_eligibility"]


def test_answer_citation_guard_and_status_checks() -> None:
    checks = score(
        case(
            answer_contains_all=["15 days"],
            answer_contains_any=["eligible", "yes"],
            answer_not_contains=["paris"],
            citation_contains_any=["Leave Policy"],
            guard="sensitive",
            status="awaiting_confirmation",
            max_tool_calls=1,
        ),
        answer="Yes,  you have 15\nDAYS left.",
        tool_uses=[{"name": "a", "ok": True}, {"name": "b", "ok": True}],
        citations=["Leave Policy v1.3, 4.2 Sick Leave (p. 2)"],
        status_before_approval="completed",
        guard="allow",
    )
    assert failed(checks) == ["status", "guard", "max_tool_calls"]


def test_public_case_file_is_valid() -> None:
    cases = load_cases(Path("evals/cases.yaml"))
    assert len(cases) >= 30
    assert len({c.id for c in cases}) == len(cases)
    for c in cases:
        assert c.employee in SEED_EMAILS, c.id
        named = (
            c.expect.tools_all + c.expect.tools_any + c.expect.tools_none + list(c.expect.tool_args)
        )
        assert all(t in TOOLS_BY_NAME for t in named), c.id
        if c.expect.status == "awaiting_confirmation":
            assert c.approve is not None, c.id
