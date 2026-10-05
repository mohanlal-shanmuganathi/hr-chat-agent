"""The eval runner end to end with a scripted model (no LLM cost), against the test database."""

from pathlib import Path

import pytest

from app.agent.runtime import build_runtime
from app.config import Settings
from app.hr.seed import SeedSummary
from evals.run_evals import load_cases, render_report, run_case
from tests.fakes import ScriptedChatModel, call, say


@pytest.fixture
def cases() -> dict[str, object]:
    return {c.id: c for c in load_cases(Path("evals/cases.yaml"))}


async def test_runner_scores_tool_case_and_confirmation_case(
    database_url: str, seeded: SeedSummary, cases: dict[str, object]
) -> None:
    model = ScriptedChatModel(
        script=[
            # E01: eligibility with the expected arguments
            call("check_leave_eligibility", leave_type="CL", start="2026-10-16", end="2026-10-20"),
            say("Yes, you are eligible: it uses 2 days of CL."),
            # A01: write action, approved by the runner
            call("submit_leave_request", leave_type="EL", start="2026-11-09", end="2026-11-10"),
            say("Your request is pending approval."),
        ]
    )
    settings = Settings(
        database_url=database_url,  # type: ignore[arg-type]
        embedding_provider="hashing",
        scope_check_enabled=False,  # the script covers only the agent's calls
    )
    async with build_runtime(settings, model=model) as rt:
        assert rt.chat is not None
        e01 = await run_case(rt, rt.chat, cases["E01"], use_judge=False, delay=0)  # type: ignore[arg-type]
        a01 = await run_case(rt, rt.chat, cases["A01"], use_judge=False, delay=0)  # type: ignore[arg-type]

    assert e01.passed, [c for c in e01.checks if not c.ok]
    assert a01.passed, [c for c in a01.checks if not c.ok]
    report = render_report([e01, a01], "Evaluation results", "scripted", show_answers=True)
    assert "Pass rate: 2/2 (100%)" in report
