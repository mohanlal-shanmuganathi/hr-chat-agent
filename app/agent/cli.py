"""Terminal chat for trying the agent before the web UI exists.

Usage:  python -m app.agent.cli --email priya.r@example.com
(Development only: it trusts the --email argument instead of a real login.)
"""

import argparse
import asyncio
import uuid

from app.agent.runtime import build_runtime
from app.agent.service import ChatError, EmployeeIdentity
from app.config import get_settings
from app.core.logging import configure_logging


async def main(email: str) -> None:
    settings = get_settings()
    configure_logging("WARNING", json=False)
    async with build_runtime(settings) as rt:
        if rt.chat is None:
            raise SystemExit(f"Chat unavailable: {rt.chat_unavailable}")
        chat = rt.chat
        emp_id = await rt.deps.hr.find_employee_id_by_email(email)
        if emp_id is None:
            raise SystemExit(f"No employee with email {email}. Run: python -m app.hr.seed")
        profile = await rt.deps.hr.get_profile(emp_id)
        me = EmployeeIdentity(emp_id, profile.full_name, profile.location.value)
        thread = uuid.uuid4().hex
        print(f"Signed in as {profile.full_name} ({profile.location.value}). Ctrl+C to quit.\n")
        while True:
            text = (await asyncio.to_thread(input, "you> ")).strip()
            if not text:
                continue
            try:
                result = await chat.send(me, thread, text, rt.today())
                while result.status == "awaiting_confirmation" and result.pending_action:
                    print(f"\n  ⚠ Confirm: {result.pending_action['summary']}")
                    answer = await asyncio.to_thread(input, "  approve? [y/N] ")
                    approved = answer.strip().lower() == "y"
                    result = await chat.resume(me, thread, approved, rt.today())
            except ChatError as exc:
                print(f"[{exc.code}] {exc.message}\n")
                continue
            print(f"\nassistant> {result.answer}\n")
            if result.citations:
                print("  sources: " + " | ".join(result.citations[:3]))
            tools = ", ".join(
                f"{t.name}{'' if t.ok else '!' + str(t.error_code)}" for t in result.tools_used
            )
            tokens = f"{result.input_tokens}/{result.output_tokens}"
            print(f"  [tools: {tools or 'none'} · tokens in/out {tokens}]\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    try:
        asyncio.run(main(parser.parse_args().email))
    except (KeyboardInterrupt, EOFError):
        print()
