"""Run the evaluation suite against the real agent (real LLM, real tools, mock HR data).

Usage (needs GOOGLE_API_KEY; resets the fictional mock HR data):
    python -m evals.run_evals [--judge] [--only E01,E02] [--delay 4] [--min-pass-rate 0.85]

Writes docs/eval-results.md for public cases and evals/private/results.md for private cases
(git-ignored). Exits non-zero if the pass rate is below --min-pass-rate.
"""

import argparse
import asyncio
import json
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.graph import message_text
from app.agent.runtime import build_runtime
from app.agent.service import ChatService, EmployeeIdentity, TurnResult
from app.config import get_settings
from app.core.logging import configure_logging
from app.hr.seed import seed
from evals.scoring import CaseOutcome, Check, EvalCase, score

EVAL_DATE = date(2026, 10, 3)
PUBLIC_CASES = Path("evals/cases.yaml")
PRIVATE_CASES = Path("evals/private/cases.yaml")
PUBLIC_REPORT = Path("docs/eval-results.md")
PRIVATE_REPORT = Path("evals/private/results.md")
RATE_LIMIT_WAIT_S = 60  # free-tier per-minute quota window

JUDGE_PROMPT = """You are grading an HR assistant's answer for groundedness.
Given the tool outputs it received and its final answer, decide whether every factual claim in
the answer (numbers, rules, dates, eligibility) is supported by the tool outputs. Ignore style.
Reply with JSON only: {"supported": true|false, "unsupported_claims": ["..."]}"""


def load_cases(path: Path) -> list[EvalCase]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [EvalCase.model_validate(c) for c in data.get("cases", [])]


async def judge(rt: Any, transcript: list[dict[str, Any]], answer: str) -> Check:
    from app.agent.llm import create_chat_model

    tool_text = "\n\n".join(
        f"[{t['name']}] {t['content'][:3000]}" for t in transcript if t["role"] == "tool"
    )
    model = create_chat_model(rt.settings)
    reply = await model.ainvoke(
        [
            SystemMessage(content=JUDGE_PROMPT),
            HumanMessage(content=f"TOOL OUTPUTS:\n{tool_text or '(none)'}\n\nANSWER:\n{answer}"),
        ]
    )
    raw = message_text(reply)
    try:
        verdict = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
        return Check("judge:grounded", bool(verdict.get("supported")), str(verdict))
    except (json.JSONDecodeError, AttributeError):
        return Check("judge:grounded", False, f"unparseable judge reply: {raw[:200]}")


async def run_case(
    rt: Any, chat: ChatService, case: EvalCase, use_judge: bool, delay: float
) -> CaseOutcome:
    """Run a case; if the free-tier rate limit cut a turn short, wait and run it once more."""
    outcome, rate_limited = await attempt_case(rt, chat, case, use_judge, delay)
    if rate_limited:
        print(f"      … {case.id} hit the rate limit; retrying in {RATE_LIMIT_WAIT_S}s")
        await asyncio.sleep(RATE_LIMIT_WAIT_S)
        outcome, _ = await attempt_case(rt, chat, case, use_judge, delay)
        outcome.note = "retried after rate limit"
    return outcome


async def attempt_case(
    rt: Any, chat: ChatService, case: EvalCase, use_judge: bool, delay: float
) -> tuple[CaseOutcome, bool]:
    """One run of a case, and whether any turn ended on the provider's rate limit."""
    outcome = CaseOutcome(case=case)
    rate_limited = False
    emp_id = await rt.deps.hr.find_employee_id_by_email(case.employee)
    if emp_id is None:
        outcome.error = f"unknown employee {case.employee}"
        return outcome, False
    profile = await rt.deps.hr.get_profile(emp_id)
    who = EmployeeIdentity(emp_id, profile.full_name, profile.location.value)
    thread = f"eval-{case.id}-{uuid.uuid4().hex[:8]}"

    tool_uses: list[dict[str, Any]] = []
    citations: list[str] = []
    status_before_approval = "completed"
    last: TurnResult | None = None
    started = time.perf_counter()
    try:
        for text in case.turns:
            last = await chat.send(who, thread, text, EVAL_DATE)
            results = [last]
            if last.status == "awaiting_confirmation":
                status_before_approval = last.status
                await asyncio.sleep(delay)
                last = await chat.resume(who, thread, bool(case.approve), EVAL_DATE)
                results.append(last)
            for r in results:
                tool_uses += [{"name": t.name, "ok": t.ok, "args": t.args} for t in r.tools_used]
                citations += r.citations
                outcome.input_tokens += r.input_tokens
                outcome.output_tokens += r.output_tokens
                rate_limited |= r.fallback == "llm_rate_limited"
            await asyncio.sleep(delay)
    except Exception as exc:  # record and continue with the next case
        outcome.error = f"{type(exc).__name__}: {exc}"[:300]
        return outcome, getattr(exc, "code", None) == "llm_rate_limited"
    finally:
        outcome.latency_s = round(time.perf_counter() - started, 1)

    assert last is not None
    outcome.answer = last.answer or ""
    outcome.tools = [t["name"] for t in tool_uses]
    outcome.checks = score(
        case,
        answer=outcome.answer,
        tool_uses=tool_uses,
        citations=citations,
        status_before_approval=status_before_approval,
        guard=last.guard,
    )
    if use_judge and case.expect.grounded:
        outcome.checks.append(await judge(rt, await chat.transcript(who, thread), outcome.answer))
    return outcome, rate_limited


def render_report(outcomes: list[CaseOutcome], title: str, model: str, show_answers: bool) -> str:
    passed = sum(o.passed for o in outcomes)
    total = len(outcomes) or 1
    by_cat: dict[str, list[CaseOutcome]] = {}
    for o in outcomes:
        by_cat.setdefault(o.case.category, []).append(o)
    latencies = sorted(o.latency_s for o in outcomes)
    median_latency = latencies[len(latencies) // 2] if latencies else 0
    lines = [
        f"# {title}",
        "",
        f"- Model: `{model}` · evaluation date (agent's 'today'): {EVAL_DATE.isoformat()}",
        f"- **Pass rate: {passed}/{len(outcomes)} ({passed / total:.0%})**",
        f"- Tokens: {sum(o.input_tokens for o in outcomes)} in / "
        f"{sum(o.output_tokens for o in outcomes)} out · "
        f"median latency {median_latency}s",
        "",
        "| Category | Passed |",
        "|---|---|",
        *[
            f"| {c} | {sum(o.passed for o in os_)}/{len(os_)} |"
            for c, os_ in sorted(by_cat.items())
        ],
        "",
        "| Case | Result | Tools | Failed checks |",
        "|---|---|---|---|",
    ]
    for o in outcomes:
        failed = o.error or "; ".join(f"{c.name} ({c.detail})"[:160] for c in o.checks if not c.ok)
        mark = ("✅" if o.passed else "❌") + (f" ({o.note})" if o.note else "")
        lines.append(
            f"| {o.case.id} | {mark} | {', '.join(o.tools) or '-'} | "
            f"{failed.replace('|', '/') or ''} |"
        )
    if show_answers:
        lines += ["", "## Answers", ""]
        for o in outcomes:
            lines += [f"**{o.case.id}** — {o.case.turns[-1]}", "", f"> {o.answer[:600]}", ""]
    return "\n".join(lines) + "\n"


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging("WARNING", json=False)
    public, private = load_cases(PUBLIC_CASES), load_cases(PRIVATE_CASES)
    if args.only:
        wanted = set(args.only.split(","))
        public = [c for c in public if c.id in wanted]
        private = [c for c in private if c.id in wanted]

    async with build_runtime(settings) as rt:
        if rt.chat is None:
            print(f"Chat unavailable: {rt.chat_unavailable}")
            return 2
        async with rt.sessions() as s:
            await seed(s, EVAL_DATE)  # deterministic fictional data
        results: dict[str, list[CaseOutcome]] = {"public": [], "private": []}
        for kind, cases in (("public", public), ("private", private)):
            for case in cases:
                outcome = await run_case(rt, rt.chat, case, args.judge, args.delay)
                results[kind].append(outcome)
                mark = "PASS" if outcome.passed else "FAIL"
                note = f" ({outcome.note})" if outcome.note else ""
                print(f"{mark} {case.id:5} {case.category:14} tools={outcome.tools}{note}")
                if not outcome.passed:
                    for c in outcome.checks:
                        if not c.ok:
                            print(f"      ✗ {c.name}: {c.detail[:150]}")
                    if outcome.error:
                        print(f"      ! {outcome.error}")

    if results["public"]:
        # No answers in the public report: they quote the confidential policies.
        report = render_report(results["public"], "Evaluation results", settings.llm_model, False)
        await asyncio.to_thread(PUBLIC_REPORT.write_text, report, encoding="utf-8")
    if results["private"]:
        report = render_report(
            results["private"], "Private evaluation results", settings.llm_model, True
        )
        await asyncio.to_thread(PRIVATE_REPORT.write_text, report, encoding="utf-8")
    everything = results["public"] + results["private"]
    rate = sum(o.passed for o in everything) / max(len(everything), 1)
    print(f"\nPass rate: {rate:.0%} ({sum(o.passed for o in everything)}/{len(everything)})")
    return 0 if rate >= args.min_pass_rate else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--judge", action="store_true", help="LLM groundedness check")
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--delay", type=float, default=4.0, help="seconds between model turns")
    parser.add_argument("--min-pass-rate", type=float, default=0.85)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
